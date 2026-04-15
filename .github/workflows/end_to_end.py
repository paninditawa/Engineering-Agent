#TODO: The code works up to the part where the lead agent gives feedback on why the code doesn't work. There are TODO statements where things still need to be done. I left off on line 290
#TODO: We really gotta add mongodb integration and work with pm agent to get the agents connected asap, shivam really emphasized this last meeting

import json
import os
import uuid
import datetime
import subprocess
from pathlib import Path
from crewai import Agent, Crew, Task
from crewai.llm import LLM
from crewai_tools import FileReadTool
import git

#TODO: I've been using 3.1 but lowk it might be worth trying 3.2 to see if it can use tools better, I've had a lot of problems with the agents messing up their outputs when trying to use tools
#llama3 cant use tools fyi
# Local LLM (Ollama)
llm = LLM(
    model="ollama/llama3.1:latest",
    base_url="http://localhost:11434"
    )

#TODO: make a new github repo and have the code push to that repo when everythings finished
# path_to_repo = ' '
# repo = git.Repo.clone_from(path_to_repo, "eng_agent_testing")

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

    #Creates a plan based off which the rest of the code is written
    #The plan is consistently pretty good and is currently being stored in plan.md
    #The plan tends to have a lot of formatting either avoid storing it as a string/text file or copy over some of the 'STRICT RULES' form the 
    #coding prompts to reduce the fancy formatting
    def create_plan(self, spec):
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
            agent=self.lead
        )
        crew = Crew(agents=[self.lead], tasks=[task])
        plan = str(crew.kickoff())
        crew.reset_memories(command_type="all")
        return str(plan)  # Return the output of the first task, which is the development plan

    #function to figure out what files need to be created for the project based off the spec and the plan made by the lead in create_plan
    #none of these files are actually created until the code is generated in generate_code, this function just determines what files need to be created and returns a list of the file names with extensions
    #IMPORTANT: the testing file is always named "tests" and is the last one in the outputted list. This is used throughout the rest of the code
    def create_necessary_files(self, spec, plan):
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
            agent=self.lead

        )
        crew_files = Crew(agents=[self.lead], tasks=[task_create_files])
        files = str(crew_files.kickoff()).split(",")
        crew_files.reset_memories(command_type="all")
        return files

    #function to generate code based on the spec and the plan created by the lead, organized into the files determined by create_necessary_files
    #the code is generated one file at a time, and after each file is generated it is written to a file (the file is created as it is written to)
    def generate_code(self, spec, plan, file):
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

                Spec:
                {spec}

                Plan:
                {plan}

                Return ONLY valid code.
            """,
            expected_output="Just code that meets the specification.",
            agent=self.dev
        )
        crew = Crew(agents=[self.dev], tasks=[task])
        result = crew.kickoff()
        crew.reset_memories(command_type="all")
        #TODO: currently the code is being written into this directory but we need to make it go into a seperate repository.
        with open(file, "w") as f:
            f.write(str(result))

        return result

    #TODO: This function is meant to be like the generate_code function but for fixing code.
    #I think that this would be best implemented by not showing the agent the code it had already written but rather have it re-write code based on the feedback from the lead but i havent tested it yet sp idk
    def fix_code(self, spec, plan, feedback=""):
        current = Path("app.py").read_text()
        task = Task(
            description=f"""
                Your lead developer has given you feedback on the code your team has written so far. Use this feedback to fix the code and ensure it meets the specifications.

                STRICT RULES:
                - Output ONLY raw Python code
                - NO markdown (no ``` )
                - NO explanations
                - NO comments unless necessary
                - The first character must be valid Python code

                The current code written by your team is not passing the tests it needs to and requires fixing.
                The code is stored in a file called "app.py". Here is the current code:
                {current}
                The specifications this code needs to meet are:
                {spec}

                Return ONLY valid Python code.
            """,
            expected_output="Only valid Python code that meets the specification.",
            agent=self.dev
        )
        crew = Crew(agents=[self.dev], tasks=[task])
        return str(crew.kickoff())
 
    
    #TODO: This function may or may not be necessary. It is currently unused but maybe having a prompt specifically for making tests could make the code less likely to have errors which would be really helpful as the tests having errors in them is a really big problem.
    def generate_tests(self, code):
        task = Task(
            description=f"""
                Write unit tests for the provided code. The code is stored in a file called "app.py".

                STRICT RULES:
                - Output ONLY raw code
                - NO syntax errors in the code you output
                - NO markdown (no ``` )
                - NO explanations
                - NO comments unless necessary
                - The first character must be valid executable code

                Code:
                {code}

                Use unittest or pytest.
                Return ONLY test code.
        """,
        expected_output="Only valid executable test code.",
            agent=self.tester
        )
        crew = Crew(agents=[self.tester], tasks=[task])
        return str(crew.kickoff())

    #TODO: This function is supposed to return True/False based on whether the tests pass followed by a description if necessary. We need to make it so that it can execute both python and js code at least, maybe also execute code in any language.
    def run_tests(self, testing_file):
        try:
            result = subprocess.run(
                ["python", testing_file],
                capture_output=True,
                text=True,
                timeout=5
            )
            print("STDOUT:", result.stdout)
            print("STDERR:", result.stderr)

            if result.returncode == 0:
                return True, "All tests passed successfully."
            else:
                return False, result.stderr
        #TODO: Add detailed error messages so the lead agent has a better idea of what went wrong when the tests fail.
        #An issue i've found is that if the tests have an infinite loop or mistakenly wait for user input, the subprocess will just hang and never return an output. To get around this, i'm setting a timeout of 5 seconds, and if the subprocess takes longer than that it will raise a TimeoutExpired error which we can catch and return a message saying that the test execution timed out.
        except subprocess.TimeoutExpired:
            return False, "Test execution timed out."
        except Exception as e:
            return False, str(e)

#Main loop of the system: create plan->generate initial code->run tests->make feedback and iterate and stuff
def review_and_iterate(self, spec, max_iterations=10):
    #The code from here to the todo works
    plan = self.create_plan(spec)
    with open("plan.md", "w") as f:
        f.write(plan)
    files = self.create_necessary_files(spec, plan)
    testing_file = files[-1]  # the testing file should be the last one in the list
    print(f"Files created: {files}")

    feedback = ""
    iteration = 0

    #Create the initial code and tests, if they all pass then the program basically works perfectly.    
    for file in files:
        code = self.generate_code(spec, plan, file)

    success_status, error_message = self.run_tests(testing_file)
    if success_status:
        return {
            "status": "success",
            "iterations": iteration
        }
    
    while True:
        iteration += 1
        #TODO: implement all the stuff with the lead giving feedback and the feedback being implemented.
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
            agent=self.lead
        )
        crew = Crew(agents=[self.lead], tasks=[find_problems_task])
        feedback = str(crew.kickoff())
        crew.reset_memories(command_type="all")
        print(f"Feedback from lead:\n{feedback}")
        return
        if iteration >= max_iterations:
            return {
                "status": "failed",
                "iterations": iteration,
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
                result = review_and_iterate(system, payload["spec"], 10)
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
    #TODO: add mongodb integration and replace this with getting an actual message from a database.
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
    "spec": "Write a python class that simulates a number guessing game. The class should have methods to start a new game, make a guess, and check if the guess is correct. The game should generate a random number between 1 and 100, and the player should have a limited number of attempts to guess the number. The class should also provide feedback on whether the guess is too high, too low, or correct."
  },
  "status": "pending",
  "error": ""
}
""")
    print("Started")
    agent = EngineeringAgent()
    response = agent.handle_message(message)

    print("\nFINAL RESPONSE:\n")
    print(json.dumps(response, indent=2))