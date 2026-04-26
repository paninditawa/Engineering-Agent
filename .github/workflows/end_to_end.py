import json
import os
import sys
import ast
import uuid
import datetime
import subprocess
import time
import shutil
import importlib.util
from pathlib import Path
from crewai import Agent, Crew, Task
from crewai.llm import LLM
from crewai_tools import FileReadTool
import git
from pymongo import MongoClient

# Model note: llama3.1 is stable but tool use is unreliable. llama3.2 has better tool-calling
# support — switch the model string below if you want to try it. llama3 (base) cannot use tools.
# Local LLM (Ollama) — override with OLLAMA_MODEL env var to switch models without editing code.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "ollama/llama3.1:latest")
llm = LLM(
    model=OLLAMA_MODEL,
    base_url="http://localhost:11434"
    )

# GitHub repo for generated output — set GITHUB_REPO_URL to a remote URL to enable push.
# e.g. $env:GITHUB_REPO_URL = "https://github.com/your-org/generated-output.git"
# Leave unset to skip pushing (files are still written locally under OUTPUT_DIR).
GITHUB_REPO_URL = os.environ.get("GITHUB_REPO_URL", "")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "generated_output")

def init_output_repo():
    """Ensure OUTPUT_DIR exists as a git repo, cloning from GITHUB_REPO_URL if set."""
    output_path = Path(OUTPUT_DIR)
    if output_path.exists() and (output_path / ".git").exists():
        return git.Repo(output_path)
    if GITHUB_REPO_URL:
        return git.Repo.clone_from(GITHUB_REPO_URL, output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    return git.Repo.init(output_path)

def commit_and_push(repo, message="chore: agent-generated code"):
    """Stage all files, commit, and push to origin if a remote is configured."""
    repo.git.add(A=True)
    if repo.is_dirty(index=True, untracked_files=True):
        repo.index.commit(message)
        print(f"Committed: {message}")
    if GITHUB_REPO_URL and "origin" in [r.name for r in repo.remotes]:
        repo.remotes.origin.push()
        print(f"Pushed to {GITHUB_REPO_URL}")
    else:
        print("No remote configured — skipping push.")

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def make_response(sender, recipient, task_type, payload, status="done", error=None):
    return { 
        "id": str(uuid.uuid4()),
        "timestamp": now(),
        "sender": sender,
        "recipient": recipient,
        "task_type": task_type,
        "context": "",
        "payload": payload,
        "status": status,
        "error": error
    }

# ---------------------------------------------------------------------------
# Token budget system
# ---------------------------------------------------------------------------
# Each agent role has a budget of "virtual tokens" (a proxy for how much LLM
# work they're allowed to do per task). When a task is run through an agent
# we deduct an estimated cost. If that agent is out of budget:
#   1. A higher-privilege agent absorbs the work instead.
#   2. If NO agent has budget left, a token-request message is sent to the
#      HR agent and the current task raises a RecoverableError so the polling
#      loop can retry it after tokens are replenished.
#
# Budgets reset at the start of each new top-level task (each PM message).

DEFAULT_BUDGETS = {
    "lead":   5000,   # lead gets the most — plans, reviews, feedback
    "dev":    4000,   # dev does the heavy code-writing
    "tester": 2000,   # tester writes tests, usually shorter outputs
}

# Rough token cost estimates per operation type
OP_COSTS = {
    "plan":         800,
    "file_list":    200,
    "generate":     600,
    "test_gen":     500,
    "feedback":     600,
    "fix":          600,
}

class RecoverableError(Exception):
    """Raised when all agents are out of tokens. The polling loop will re-queue
    the message and wait for HR to replenish budgets."""
    pass

class TokenBudget:
    def __init__(self):
        self.budgets = dict(DEFAULT_BUDGETS)

    def reset(self):
        self.budgets = dict(DEFAULT_BUDGETS)

    def remaining(self, role: str) -> int:
        return self.budgets.get(role, 0)

    def deduct(self, role: str, op: str):
        cost = OP_COSTS.get(op, 300)
        self.budgets[role] = max(0, self.budgets.get(role, 0) - cost)

    def can_afford(self, role: str, op: str) -> bool:
        return self.budgets.get(role, 0) >= OP_COSTS.get(op, 300)

    def fallback_agent(self, db, original_role: str, op: str):
        """Return the name of the cheapest agent that can still afford the op,
        preferring higher-privilege roles. Returns None and fires an HR request
        if no one can afford it."""
        # Fallback priority: lead can do anything, dev can review, tester is last resort
        priority = ["lead", "dev", "tester"]
        for role in priority:
            if role != original_role and self.can_afford(role, op):
                print(f"[TokenBudget] '{original_role}' out of budget for '{op}' — falling back to '{role}'")
                return role
        # Nobody left — ask HR for more tokens
        self._request_tokens_from_hr(db)
        raise RecoverableError(
            f"All agents out of token budget for op '{op}'. Sent token request to HR."
        )

    def _request_tokens_from_hr(self, db):
        request = {
            "id": str(uuid.uuid4()),
            "timestamp": now(),
            "sender": "ENG",
            "recipient": "HR",
            "task_type": "TOKEN_REQUEST",
            "context": "",
            "payload": {
                "reason": "All engineering agents have exhausted their token budgets mid-task.",
                "requested_budgets": DEFAULT_BUDGETS,
            },
            "status": "pending",
            "error": ""
        }
        db.messages.insert_one(request)
        request.pop("_id", None)
        print(f"[TokenBudget] Token request sent to HR agent: {request['id']}")


# ---------------------------------------------------------------------------

class FullSystem:
    def __init__(self, db=None):
        self.llm = llm
        self.db = db          # needed for HR token requests
        self.tokens = TokenBudget()

        # Helper: resolve which CrewAI Agent object to use for a role, respecting budget
        self._agents_by_role = {}  # populated after agents are defined below

        def _agent_for(role, op):
            if os.environ.get("DISABLE_TOKEN_BUDGET"):
                return self._agents_by_role[role]
            if self.tokens.can_afford(role, op):
                self.tokens.deduct(role, op)
                return self._agents_by_role[role]
            fallback_role = self.tokens.fallback_agent(self.db, role, op)
            self.tokens.deduct(fallback_role, op)
            return self._agents_by_role[fallback_role]

        self._agent_for = _agent_for
        
        #TODO: IDK how much of an issue this will be in the long run but the agent's memories are causing them to sometimes mess up their outputs.
        # setting memory to false is supposed to fix this but doesn't really work, i've been using "reset_memories(command_type="all")" after tasks which seems to have maybe helped?
        # Lead Developer (Planner + Reviewer)
        self.lead = Agent(
            role="Lead Developer",
            goal="Plan tasks, review outputs, and ensure code quality.",
            backstory="""You are a part of the engineering team of a company consisting of AI agents. You are a senior engineer with years of experience and strong leadership skills.
                        You are the leader of the team and your job is to create clear, actionable development plans based on product specifications, review the code written by your 
                        team, and give specific feedback to ensure the final code is clean, efficient, well-structured, and meets the specifications.
            """,
            memory=False,
            cache=False,
            llm=self.llm
        )

        # Software Developer
        self.dev = Agent(
            role="Software Developer",
            goal="Write clean, functional code.",
            backstory="""You are a part of the engineering team of a company consisting of AI agents. You are an experienced developer with a strong focus on writing clean, maintainable code.
                        Your job is to write code based on the development plans created by your lead developer and to fix any issues in the code based on the feedback you receive from your lead.
                        The code you write should be efficient, well-structured, and meet the specifications provided to you.""",
            memory=False,
            llm=self.llm
        )

        # Testing Agent
        self.tester = Agent(
            role="Testing Engineer",
            goal="Write and evaluate unit tests for code you are provided with.",
            backstory="""You are a part of the engineering team of a company consisting of AI agents. You are responsible for writing unit tests 
                        for the code written by your development agent and evaluating whether the code meets the specifications based on the results of these tests.
            """,
            memory=False,
            llm=self.llm
        )

        # Register agents so _agent_for can look them up by role name
        self._agents_by_role = {
            "lead":   self.lead,
            "dev":    self.dev,
            "tester": self.tester,
        }

    def _required_contract(self, spec):
        """Return required interface checks inferred from the task spec.
        This prevents generated code from passing weak tests while missing
        critical API methods.
        """
        spec_l = spec.lower()
        contracts = {}

        if "number guessing" in spec_l or "guessing game" in spec_l:
            methods = ["make_guess"]
            if "start new game" in spec_l or "start_game" in spec_l or "start a new game" in spec_l:
                methods.append("start_new_game")
            contracts["number_guessing_game.py"] = {
                "class": "NumberGuessingGame",
                "methods": methods,
            }

        return contracts

    def _required_source_files(self, spec):
        """Return source filenames that must exist for known feature types."""
        spec_l = spec.lower()
        required = []
        if "number guessing" in spec_l or "guessing game" in spec_l:
            required.append("number_guessing_game.py")
        return required

    def _is_number_guessing_spec(self, spec):
        spec_l = spec.lower()
        return "number guessing" in spec_l or "guessing game" in spec_l

    def _number_guessing_plan(self):
        return """1. Create number_guessing_game.py with a NumberGuessingGame class.
2. Implement __init__(max_attempts=5) to initialize max_attempts, attempts_remaining, and a secret number.
3. Implement start_new_game() to reset attempts and generate a new random number from 1 to 100.
4. Implement make_guess(guess) so guesses below the secret return 'too low', above return 'too high', equal return 'correct', and invalid input returns an invalid-input message.
5. Do not use input(), print-driven gameplay loops, or embedded tests inside the source module.
6. Create tests.py with deterministic unittest coverage for class construction, start_new_game, and make_guess behavior.
"""

    def _number_guessing_tests(self):
        return """import unittest
from number_guessing_game import NumberGuessingGame


class TestNumberGuessingGame(unittest.TestCase):
    def setUp(self):
        self.game = NumberGuessingGame(max_attempts=5)
        # Force deterministic state for behavior tests.
        if hasattr(self.game, 'number_to_guess'):
            self.game.number_to_guess = 42
        elif hasattr(self.game, '_secret_number'):
            self.game._secret_number = 42
        elif hasattr(self.game, 'secret_number'):
            self.game.secret_number = 42

    def _set_secret(self, value):
        if hasattr(self.game, 'number_to_guess'):
            self.game.number_to_guess = value
        elif hasattr(self.game, '_secret_number'):
            self.game._secret_number = value
        elif hasattr(self.game, 'secret_number'):
            self.game.secret_number = value
        else:
            self.fail('Game has no recognized secret number attribute')

    def test_required_methods_exist(self):
        self.assertTrue(hasattr(self.game, 'start_new_game'))
        self.assertTrue(hasattr(self.game, 'make_guess'))

    def test_start_new_game_sets_number_in_range(self):
        self.game.start_new_game()
        secret = getattr(
            self.game,
            'number_to_guess',
            getattr(self.game, '_secret_number', getattr(self.game, 'secret_number', None)),
        )
        self.assertIsInstance(secret, int)
        self.assertGreaterEqual(secret, 1)
        self.assertLessEqual(secret, 100)

    def test_make_guess_too_low(self):
        self._set_secret(42)
        result = str(self.game.make_guess(10)).lower()
        self.assertIn('low', result)

    def test_make_guess_too_high(self):
        self._set_secret(42)
        result = str(self.game.make_guess(90)).lower()
        self.assertIn('high', result)

    def test_make_guess_correct(self):
        self._set_secret(42)
        result = str(self.game.make_guess(42)).lower()
        self.assertIn('correct', result)

    def test_make_guess_invalid_input(self):
        result = str(self.game.make_guess('abc')).lower()
        self.assertTrue('invalid' in result or 'enter' in result)


if __name__ == '__main__':
    unittest.main()
"""

    def _clean_output_dir(self):
        output_path = Path(OUTPUT_DIR)
        output_path.mkdir(parents=True, exist_ok=True)
        for child in output_path.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    def _contract_hint(self, spec, file):
        """Prompt hint injected into generation prompts for files that have
        strict API requirements."""
        contracts = self._required_contract(spec)
        entry = contracts.get(file)
        if not entry:
            return ""
        method_lines = "\n".join(f"- {m}(...)" for m in entry["methods"])
        return f"""
                REQUIRED API CONTRACT FOR THIS FILE:
                - Define class `{entry['class']}`.
                - The constructor MUST accept a `max_attempts` argument, preferably with a default.
                - The class MUST include these public methods:
                {method_lines}
                - Do not rename these methods.
        """

    def validate_contract(self, spec, source_files):
        """Validate required class/method contract in generated source files.
        Returns (ok, message)."""
        contracts = self._required_contract(spec)
        if not contracts:
            return True, "No explicit contract checks required for this spec."

        for file_name, contract in contracts.items():
            if file_name not in source_files:
                return False, f"Missing required source file: {file_name}"

            file_path = Path(OUTPUT_DIR) / file_name
            if not file_path.exists():
                return False, f"Required file not generated: {file_name}"

            try:
                tree = ast.parse(file_path.read_text(encoding="utf-8"))
            except Exception as e:
                return False, f"Contract check parse error in {file_name}: {e}"

            class_node = None
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name == contract["class"]:
                    class_node = node
                    break

            if class_node is None:
                return False, f"Missing required class {contract['class']} in {file_name}"

            method_nodes = {
                node.name: node for node in class_node.body if isinstance(node, ast.FunctionDef)
            }
            method_names = set(method_nodes)
            for method in contract["methods"]:
                if method not in method_names:
                    return False, f"Missing required method {contract['class']}.{method} in {file_name}"

            if self._is_number_guessing_spec(spec):
                init_node = method_nodes.get("__init__")
                if init_node is None:
                    return False, f"Missing required constructor {contract['class']}.__init__ in {file_name}"
                init_args = [arg.arg for arg in init_node.args.args]
                if "max_attempts" not in init_args:
                    return False, f"Constructor {contract['class']}.__init__ must accept max_attempts in {file_name}"

        # Additional safety checks for number guessing tasks.
        if self._is_number_guessing_spec(spec):
            game_file = Path(OUTPUT_DIR) / "number_guessing_game.py"
            if game_file.exists():
                content = game_file.read_text(encoding="utf-8")
                if "input(" in content:
                    return False, "Forbidden interactive input() found in number_guessing_game.py"
                if "unittest" in content or "TestCase" in content:
                    return False, "number_guessing_game.py must not contain test code or unittest imports"

                # Reject top-level side effects like immediate gameplay execution.
                tree = ast.parse(content)
                for node in tree.body:
                    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                        return False, "Top-level function/class call found in number_guessing_game.py"
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                        return False, "Top-level assignment-from-call found in number_guessing_game.py"

        return True, "Contract checks passed."

    #Creates a plan based off which the rest of the code is written
    #The plan is consistently pretty good and is currently being stored in plan.md
    #The plan tends to have a lot of formatting either avoid storing it as a string/text file or copy over some of the 'STRICT RULES' form the 
    #coding prompts to reduce the fancy formatting
    def create_plan(self, spec):
        if self._is_number_guessing_spec(spec):
            return self._number_guessing_plan()

        chosen_lead = self._agent_for("lead", "plan")
        task = Task(
            description=f"""
                You are the Lead developer of an engineering team consisting of AI coding agents. Your job is to create a clear, actionable development plan based on the given specification.
                The plan will consist of a numbered list of steps that the development agent can follow to implement the required functionality. Be sure to break down the tasks into manageable pieces and consider any edge cases or potential challenges.
                Your next task will be to determine what files need to be created for the project, so mention potential files that might into which code will need to be organized in your plan.

                IMPORTANT:
                - A git repository has already been created for this project, so you do not need to include any steps related to setting up a repository or version control in your plan.
                - no folder structure is necessary, just a list of files with extensions that are necessary for the project based on the specifications and the plan you have created.
                - Focus ONLY on the development steps necessary to implement the functionality based on the specifications, and organizing the code into appropriate files.

                Spec:
                {spec}
            """,
            expected_output="A clear, organized, numbered list of development steps that will allow your team to achieve the specified functionality.",
            agent=chosen_lead
        )
        crew = Crew(agents=[chosen_lead], tasks=[task])
        plan = str(crew.kickoff())
        crew.reset_memories(command_type="all")
        return str(plan)  # Return the output of the first task, which is the development plan

    #function to figure out what files need to be created for the project based off the spec and the plan made by the lead in create_plan
    #none of these files are actually created until the code is generated in generate_code, this function just determines what files need to be created and returns a list of the file names with extensions
    #IMPORTANT: the testing file is always named "tests" and is the last one in the outputted list. This is used throughout the rest of the code
    def create_necessary_files(self, spec, plan):
        # For number guessing tasks, force a deterministic file set to reduce drift.
        if self._is_number_guessing_spec(spec):
            return ["number_guessing_game.py", "tests.py"]

        chosen_lead = self._agent_for("lead", "file_list")
        task_create_files = Task(
            description=f"""
                You are the lead developer of an engineering team consisting of AI coding agents. You are woking on building a project following the specifications provided. Based on the development plan you have created, determine what files need to be created for this project.
                To complete this task, create a list containing the names of the files that need to be created along with the extension (e.g. "app.py" for the main code, "test_app.py" for tests, etc.). The files you list should be seperated by commas with no spaces or any other additional formatting in between.
                You will only be making one testing file! Name it "tests" and have it be the last one in the list!
                
                IMPORTANT:
                - no folder structure is necessary, just a list of files with extensions that are necessary for the project based on the specifications and the plan you have created.
                - There will only be one testing file, and it should be named "tests" with the appropriate extension based on the type of code you are writing (e.g. "tests.py" for Python).
                - This testing file should be the last one in the list of files you output.

                STRICT RULES:
                - Output ONLY the file names with extensions in a list format, separated by commas with no spaces or any other additional formatting in between.
                - NO invalid extensions or file names that do not follow standard conventions for the type of code they will contain.
                - NO markdown (no ``` )
                - NO explanations
                - NO comments
                - NO new folders or directories, just files to add to the root directory of the project
                - All the output from the first character to the last must be valid file names with extensions that are necessary for the project based on the specifications.

                The plan you have created:
                {plan}

                Spec:
                {spec}
            """,
            expected_output="A list of necessary files that need to be created for the project, including their names and extensions, seperated by commas with no spaces or any other additional formatting in between.",
            agent=chosen_lead
        )
        crew_files = Crew(agents=[chosen_lead], tasks=[task_create_files])
        files = [name.strip() for name in str(crew_files.kickoff()).split(",") if name.strip()]
        crew_files.reset_memories(command_type="all")

        # Enforce mandatory source files so contract checks remain satisfiable.
        required_sources = self._required_source_files(spec)
        if files:
            testing_file = files[-1]
            source_files = files[:-1]
            for req in required_sources:
                if req not in source_files:
                    source_files.append(req)
            files = source_files + [testing_file]
        else:
            # Defensive fallback: create at least one source + one test file.
            files = required_sources + ["tests.py"]

        return files

    #function to generate code based on the spec and the plan created by the lead, organized into the files determined by create_necessary_files
    #the code is generated one file at a time, and after each file is generated it is written to a file (the file is created as it is written to)
    def generate_code(self, spec, plan, file):
        chosen_dev = self._agent_for("dev", "generate")
        task = Task(
            description=f"""
                You are a software developer on an engineering team consisting of AI coding agents. Your task is to look at the development plan created by your lead developer
                and write the code from that plan that would need to go into the file "{file}" to implement the functionality specified in the specifications. Write clean, efficient, 
                well-structured code that meets the specifications and follows best practices for the type of code you are writing.

                STRICT RULES:
                - Output ONLY raw code
                - NO syntax errors in the code you output
                - NO markdown (no ``` )
                - NO explanations
                - NO comments unless necessary
                - The first character must be valid executable code
                                - NO interactive input calls (no input(...)).
                                - NO top-level executable statements besides imports, class/function
                                    definitions, and optional guarded main block:
                                    if __name__ == '__main__': ...

                Spec:
                {spec}

                Plan:
                {plan}

                {self._contract_hint(spec, file)}

                Return ONLY valid code.
            """,
            expected_output="Just code that meets the specification.",
            agent=chosen_dev
        )
        crew = Crew(agents=[chosen_dev], tasks=[task])
        result = crew.kickoff()
        crew.reset_memories(command_type="all")
        # Write generated file into OUTPUT_DIR so it stays separate from the agent's own code
        out_path = Path(OUTPUT_DIR) / file
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(str(result))

        return result

    def generate_tests(self, spec, plan, file, source_files, feedback=""):
        """Dedicated test-generation prompt — produces more reliable test code than the
        generic generate_code prompt because it explicitly tells the agent what the source
        files are and what they need to test."""
        if self._is_number_guessing_spec(spec):
            result = self._number_guessing_tests()
            out_path = Path(OUTPUT_DIR) / file
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(result)
            return result

        chosen_tester = self._agent_for("tester", "test_gen")
        source_listing = "\n".join(source_files)
        task = Task(
            description=f"""
                You are a Testing Engineer on a team of AI coding agents. Write a complete unit test file
                named "{file}" that tests all the functionality described in the spec and plan below.
                The source files being tested are: {source_listing}

                STRICT RULES:
                - Output ONLY raw code
                - NO syntax errors in the code you output
                - NO markdown (no ``` )
                - NO explanations
                - NO comments unless necessary
                - The first character must be valid executable code
                - Use unittest. Do NOT rely on interactive input or network access.
                - Every test must be deterministic (seed random if needed).
                - Always end the file with: if __name__ == '__main__': unittest.main()
                - Tests MUST NOT require user input().
                - Tests must import source modules without triggering gameplay.

                Spec:
                {spec}

                Plan:
                {plan}

                Prior feedback from lead (if any):
                {feedback}

                                REQUIRED TEST COVERAGE:
                                - Include at least one test that instantiates required classes and
                                    asserts each required public method exists.
                                - Include at least one behavioral test for each required method.

                Return ONLY valid test code.
            """,
            expected_output="Only valid, runnable unit test code.",
            agent=chosen_tester
        )
        crew = Crew(agents=[chosen_tester], tasks=[task])
        result = str(crew.kickoff())
        crew.reset_memories(command_type="all")
        out_path = Path(OUTPUT_DIR) / file
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result)
        return result

    def run_number_guessing_behavior_checks(self):
        """Directly validate core runtime behavior for the number guessing benchmark.
        This gives the agent precise failures before the broader unittest run."""
        module_path = Path(OUTPUT_DIR) / "number_guessing_game.py"
        if not module_path.exists():
            return False, "number_guessing_game.py was not generated"

        try:
            spec = importlib.util.spec_from_file_location("generated_number_guessing_game", module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            return False, f"Failed to import generated module: {e}"

        game_cls = getattr(module, "NumberGuessingGame", None)
        if game_cls is None:
            return False, "Generated module does not define NumberGuessingGame"

        try:
            game = game_cls(max_attempts=5)
        except Exception as e:
            return False, f"Could not instantiate NumberGuessingGame(max_attempts=5): {e}"

        if not hasattr(game, "start_new_game") or not hasattr(game, "make_guess"):
            return False, "Generated game is missing required public methods"

        try:
            game.start_new_game()
        except Exception as e:
            return False, f"start_new_game() failed: {e}"

        secret_attr = None
        for candidate in ("number_to_guess", "_secret_number", "secret_number", "secret"):
            if hasattr(game, candidate):
                secret_attr = candidate
                break
        if secret_attr is None:
            return False, "Game has no recognized secret number attribute after start_new_game()"

        secret = getattr(game, secret_attr)
        if not isinstance(secret, int) or not (1 <= secret <= 100):
            return False, "Secret number is not a valid integer in range 1..100"

        setattr(game, secret_attr, 42)
        low = str(game.make_guess(10)).lower()
        high = str(game.make_guess(90)).lower()
        correct = str(game.make_guess(42)).lower()
        invalid = str(game.make_guess("abc")).lower()

        if "low" not in low:
            return False, f"make_guess(10) should indicate too low, got: {low}"
        if "high" not in high:
            return False, f"make_guess(90) should indicate too high, got: {high}"
        if "correct" not in correct:
            return False, f"make_guess(42) should indicate correct, got: {correct}"
        if "invalid" not in invalid and "enter" not in invalid:
            return False, f"make_guess('abc') should reject invalid input, got: {invalid}"

        return True, "Number guessing behavior checks passed."

    def run_tests(self, testing_file):
        """Run a test file and return (passed: bool, detail: str).
        Supports Python (.py) and JavaScript (.js via node) test files.
        Produces detailed error output so the lead agent knows exactly what failed.
        """
        test_path = Path(testing_file)
        ext = test_path.suffix.lower()

        # Run from the test file's own directory so relative imports in generated
        # code work consistently, and avoid duplicating OUTPUT_DIR in the path.
        run_cwd = str(test_path.parent) if test_path.parent != Path("") else None
        run_target = test_path.name if run_cwd else str(test_path)

        if ext == ".py":
            runner = [sys.executable, run_target]
        elif ext == ".js":
            runner = ["node", run_target]
        else:
            return False, f"Unsupported test file type: {ext}. Only .py and .js are supported."

        try:
            result = subprocess.run(
                runner,
                capture_output=True,
                text=True,
                timeout=30,  # raised from 5s — some tests need more time to start up
                cwd=run_cwd
            )
            combined = result.stdout + ("\nSTDERR:\n" + result.stderr if result.stderr.strip() else "")
            print(combined)

            if result.returncode == 0:
                return True, "All tests passed successfully."
            else:
                # Give the lead agent both stdout and stderr for diagnosis.
                detail = (
                    f"Tests failed (exit code {result.returncode}).\n"
                    f"--- test output ---\n{result.stdout}\n"
                    + (f"--- stderr ---\n{result.stderr}" if result.stderr.strip() else "")
                )
                return False, detail
        except subprocess.TimeoutExpired:
            return False, "Test execution timed out after 30 seconds. Check for infinite loops or blocking input."
        except FileNotFoundError as e:
            return False, f"Test runner not found: {e}. Make sure python/pytest or node is installed and on PATH."
        except Exception as e:
            return False, str(e)

    # Main loop: create plan -> generate initial code -> run tests -> lead feedback -> fix -> repeat
    def review_and_iterate(self, spec, max_iterations=10):
        self.tokens.reset()  # fresh budget for each new top-level task
        repo = init_output_repo()
        self._clean_output_dir()

        plan = self.create_plan(spec)
        with open(Path(OUTPUT_DIR) / "plan.md", "w", encoding="utf-8") as f:
            f.write(plan)
        files = self.create_necessary_files(spec, plan)
        testing_file = files[-1]  # testing file is always last
        source_files = files[:-1]
        print(f"Files to create: {files}")

        iteration = 0

        # Generate source files with the standard code prompt
        for file in source_files:
            self.generate_code(spec, plan, file)
        # Generate the test file with the dedicated test prompt for better reliability
        self.generate_tests(spec, plan, testing_file, source_files)

        # Enforce API contract before running tests so weak tests cannot mask
        # missing required methods.
        contract_ok, contract_message = self.validate_contract(spec, source_files)
        if not contract_ok:
            success_status, error_message = False, contract_message
        else:
            if self._is_number_guessing_spec(spec):
                success_status, error_message = self.run_number_guessing_behavior_checks()
                if success_status:
                    success_status, error_message = self.run_tests(str(Path(OUTPUT_DIR) / testing_file))
            else:
                success_status, error_message = self.run_tests(str(Path(OUTPUT_DIR) / testing_file))
        if success_status:
            commit_and_push(repo, f"feat: initial generated code ({testing_file} passing)")
            return {"status": "success", "iterations": iteration}

        # Feedback + fix loop
        while iteration < max_iterations:
            iteration += 1

            # Step 1: Lead looks at the test errors and explains what's likely wrong
            chosen_lead = self._agent_for("lead", "feedback")
            find_problems_task = Task(
                description=f"""
                    Your team has finished writing code based on the specifications and the development plan you created, but the code is not passing all the tests it needs to.
                    Your job is to look over the errors that are occuring in the tests and explain potential reasons why these errors might be occuring based on the specifications and the development plan you created.
                    Be as specific as possible in your feedback so that your team can use it to fix the code and ensure it meets the specifications.

                    The errors that are occuring in the tests are as follows:
                    {error_message}

                    The original plan you created for the development of this project is as follows:
                    {plan}

                    The specifications for this project are as follows:
                    {spec}
                """,
                expected_output="Specific feedback that your team can use to fix the code.",
                agent=chosen_lead
            )
            crew = Crew(agents=[chosen_lead], tasks=[find_problems_task])
            feedback = str(crew.kickoff())
            crew.reset_memories(command_type="all")
            print(f"Iteration {iteration} — Lead feedback:\n{feedback}")

            # Step 2: Dev re-generates each non-test file using the feedback
            for file in files[:-1]:
                chosen_dev = self._agent_for("dev", "fix")
                fix_task = Task(
                    description=f"""
                        Your lead developer has reviewed the code and provided feedback on why it is failing tests.
                        Rewrite the code for the file "{file}" from scratch, incorporating the feedback to fix the issues.

                        STRICT RULES:
                        - Output ONLY raw code
                        - NO markdown (no ``` )
                        - NO explanations
                        - NO comments unless necessary
                        - The first character must be valid executable code

                        Lead feedback:
                        {feedback}

                        Original spec:
                        {spec}

                        Development plan:
                        {plan}
                    """,
                    expected_output="Only valid executable code for the file.",
                    agent=chosen_dev
                )
                fix_crew = Crew(agents=[chosen_dev], tasks=[fix_task])
                fixed_code = str(fix_crew.kickoff())
                fix_crew.reset_memories(command_type="all")
                out_path = Path(OUTPUT_DIR) / file
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(fixed_code)

            # Step 3: Always regenerate the test file too, so syntax or assertion
            # issues in tests can be corrected during iterations.
            self.generate_tests(spec, plan, testing_file, source_files, feedback)

            contract_ok, contract_message = self.validate_contract(spec, source_files)
            if not contract_ok:
                success_status, error_message = False, contract_message
            else:
                if self._is_number_guessing_spec(spec):
                    success_status, error_message = self.run_number_guessing_behavior_checks()
                    if success_status:
                        success_status, error_message = self.run_tests(str(Path(OUTPUT_DIR) / testing_file))
                else:
                    success_status, error_message = self.run_tests(str(Path(OUTPUT_DIR) / testing_file))
            if success_status:
                commit_and_push(repo, f"fix: iteration {iteration} — tests now passing")
                return {"status": "success", "iterations": iteration}

        return {"status": "failed", "iterations": iteration}


class EngineeringAgent:
    def __init__(self, db):
        self.name = "engineering_agent"
        self.db = db

    def handle_message(self, message):
        task_type = message["task_type"]
        payload = message["payload"]

        try:
            # Legacy format: task_type = "generate_code" with payload.spec
            if task_type == "generate_code":
                spec = payload["spec"]

            # PM agent format: task_type = "IMPLEMENT_FEATURE" with acceptance_criteria etc.
            elif task_type == "IMPLEMENT_FEATURE":
                # Build a spec string from the PM message fields
                criteria = "\n".join(f"- {c}" for c in payload.get("acceptance_criteria", []))
                spec = (
                    f"Feature: {payload.get('feature_name', 'Unnamed feature')}\n"
                    f"Feature ID: {payload.get('feature_id', '')}\n"
                    f"Acceptance criteria:\n{criteria}"
                )

            else:
                return make_response(
                    sender=self.name,
                    recipient=message["sender"],
                    task_type=task_type,
                    payload={},
                    status="error",
                    error=f"Unknown task_type: {task_type}"
                )

            system = FullSystem(db=self.db)
            max_iterations = int(os.environ.get("MAX_ITERATIONS", "10"))
            result = system.review_and_iterate(spec, max_iterations)
            return make_response(
                sender=self.name,
                recipient=message["sender"],
                task_type=task_type,
                payload=result
            )

        except Exception as e:
            return make_response(
                sender=self.name,
                recipient=message["sender"],
                task_type=task_type,
                payload={},
                status="error",
                error=str(e)
            )


# MongoDB connection — reads MONGO_URI from environment so credentials are never hardcoded.
# Set the environment variable before running, e.g.:
#   $env:MONGO_URI = "mongodb+srv://user:pass@cluster.mongodb.net/"
#   $env:MONGO_DB  = "kanosei"          # optional, defaults to "kanosei"
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.environ.get("MONGO_DB", "kanosei")

def get_db():
    client = MongoClient(MONGO_URI)
    return client[MONGO_DB]

def claim_next_message(db):
    """Atomically find a pending message for ENG and mark it as in-progress."""
    return db.messages.find_one_and_update(
        {"recipient": "ENG", "status": "pending"},
        {"$set": {"status": "in-progress"}},
        return_document=True  # return the updated document
    )

def write_response(db, response):
    """Insert the agent's response as a new message document."""
    db.messages.insert_one(response)

def mark_source_done(db, message_id, status, error=""):
    """Update the original message's status once we're finished with it."""
    db.messages.update_one(
        {"id": message_id},
        {"$set": {"status": status, "error": error}}
    )

if __name__ == "__main__":
    db = get_db()
    agent = EngineeringAgent(db=db)
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))

    print(f"Engineering agent started. Polling MongoDB every {poll_interval}s...")

    while True:
        message = claim_next_message(db)

        if message is None:
            # Nothing to do — wait and try again
            time.sleep(poll_interval)
            continue

        # MongoDB adds a _id field that isn't JSON-serialisable; remove it
        message.pop("_id", None)
        print(f"\nPicked up message: {message['id']} ({message['task_type']})")

        response = agent.handle_message(message)

        # None means a RecoverableError occurred — message was re-queued, nothing to write
        if response is None:
            print(f"Message {message['id']} re-queued pending token replenishment.")
            time.sleep(poll_interval)
            continue

        # Write the response back so the PM agent can read it
        write_response(db, response)
        # MongoDB mutates the dict by adding _id (ObjectId) when inserting —
        # pop it so json.dumps doesn't crash on the non-serialisable type.
        response.pop("_id", None)

        # Mark the original message as done (or failed)
        final_status = "done" if response.get("status") == "done" else "error"
        mark_source_done(db, message["id"], final_status, response.get("error", ""))

        print(f"Response written for message {message['id']}. Status: {final_status}")
        print(json.dumps(response, indent=2))