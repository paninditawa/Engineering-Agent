import json
import uuid
import datetime
import subprocess
from pathlib import Path
from crewai import Agent, Crew, Task
from crewai.llm import LLM

# Local LLM (Ollama)
llm = LLM(
    model="ollama/phi3:latest",
    base_url="http://localhost:11434"
    )

def now():
    return datetime.datetime.utcnow().isoformat()

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

class FullSystem:
    def __init__(self):
        self.llm = llm

        # Lead Developer (Planner + Reviewer)
        self.lead = Agent(
            role="Lead Developer",
            goal="Plan tasks, review outputs, and ensure code quality.",
            backstory="You are a senior engineer overseeing development.",
            llm=self.llm
        )

        # Software Developer
        self.dev = Agent(
            role="Software Developer",
            goal="Write clean, functional Python code.",
            backstory="You are an experienced Python developer.",
            llm=self.llm
        )

        # Testing Agent
        self.tester = Agent(
            role="Testing Engineer",
            goal="Write and evaluate unit tests for code.",
            backstory="You ensure code reliability through testing.",
            llm=self.llm
        )

    def create_plan(self, spec):
        task = Task(
            description=f"""
Break this requirement into a clear step-by-step development plan.

Spec:
{spec}

Return a numbered plan.
""",
            agent=self.lead
        )
        crew = Crew(agents=[self.lead], tasks=[task])
        return str(crew.kickoff())


    def generate_code(self, spec, plan, feedback=""):
        task = Task(
            description=f"""
Write Python code based on the spec and plan.

Spec:
{spec}

Plan:
{plan}

If there is feedback, fix the issues:
{feedback}

Return ONLY valid Python code.
""",
            agent=self.dev
        )
        crew = Crew(agents=[self.dev], tasks=[task])
        return str(crew.kickoff())


    def generate_tests(self, code):
        task = Task(
            description=f"""
Write unit tests for the following Python code.

Code:
{code}

Use unittest or pytest.
Return ONLY test code.
""",
            agent=self.tester
        )
        crew = Crew(agents=[self.tester], tasks=[task])
        return str(crew.kickoff())


    def run_tests(self, code, tests):
        Path("app.py").write_text(code)
        Path("test_app.py").write_text(tests)

        try:
            result = subprocess.run(
                ["python", "test_app.py"],
                capture_output=True,
                text=True
            )
            return result.stdout, result.stderr
        except Exception as e:
            return "", str(e)


def review_and_iterate(self, spec, max_iterations=10):
    plan = self.create_plan(spec)

    feedback = ""
    iteration = 0

    while True:
        iteration += 1

        code = self.generate_code(spec, plan, feedback)
        tests = self.generate_tests(code)

        stdout, stderr = self.run_tests(code, tests)

        review_task = Task(
            description=f"""
You are the Lead Developer.

Review the test results.

STDOUT:
{stdout}

STDERR:
{stderr}

If everything is correct and production-ready, respond ONLY with:
APPROVED

Otherwise:
- Explain what is wrong
- Give specific fixes
""",
            agent=self.lead
        )

        crew = Crew(agents=[self.lead], tasks=[review_task])
        review = str(crew.kickoff())

        print("REVIEW:", review)

        if "APPROVED" in review:
            return {
                "status": "success",
                "plan": plan,
                "code": code,
                "tests": tests,
                "iterations": iteration
            }

        feedback = review

        if iteration >= max_iterations:
            return {
                "status": "failed",
                "plan": plan,
                "last_feedback": feedback,
                "iterations": iteration
            }

class EngineeringAgent:
    def __init__(self):
        self.name = "engineering_agent"

    def handle_message(self, message):
        task_type = message["task_type"]
        payload = message["payload"]

        try:
            if task_type == "generate_code":
                system = FullSystem()
                result = system.review_and_iterate(payload["spec"])

                return make_response(
                    sender=self.name,
                    recipient=message["sender"],
                    task_type=task_type,
                    payload=result
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

        except Exception as e:
            return make_response(
                sender=self.name,
                recipient=message["sender"],
                task_type=task_type,
                payload={},
                status="error",
                error=str(e)
            )


# test run
if __name__ == "__main__":
    message = json.loads("""
{
  "id": "req-002",
  "timestamp": "2026-03-02T16:30:00Z",
  "sender": "PM",
  "recipient": "ENG",
  "task_type": "generate_code",
  "context": {
    "priority": "high",
    "target_release": "2026-04-15"
  },
  "payload": {
    "spec": "Write a calculator that can add, subtract, multiply, and divide two numbers with a simple command-line interface."
  },
  "status": "pending",
  "error": ""
}
""")

    agent = EngineeringAgent()
    response = agent.handle_message(message)

    print("\nFINAL RESPONSE:\n")
    print(json.dumps(response, indent=2))