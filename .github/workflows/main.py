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

class EngineeringAgent:
    def __init__(self):
        self.name = "engineering_agent"
        self.agent = Agent(
            role="Engineering Agent",
            goal="Write and maintain high‑quality code based on product specs.",
            backstory="You are the engineering team of a virtual company.",
            llm=llm
        )

    def generate_code(self, spec):
        prompt = f"Write clean, minimal python code for the following requirement:\n\n{spec}\n\nReturn ONLY code."
        task = Task( 
            description=(prompt),
            expected_output="a string of python code that implements the specified functionality",
            agent=self.agent
            # tools=[search_tool] # Assign tools if needed
        )
        crew = Crew(
            agents = [self.agent],
            tasks = [task],
            verbose = True
        )        
        inputs = {
            "topic_context": "CrewAI is a framework for building multi-agent systems."
        }
        result = crew.kickoff(inputs=inputs)
        return result

    def write_to_repo(self, file_path, content):
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return str(path)

    def handle_message(self, message):
        task_type = message["task_type"]
        payload = message["payload"]

        try:
            print("here")
            if task_type == "generate_code":
                code = self.generate_code(payload["spec"])
                print("here")
                #file_path = payload.get("repo_path", "generated/code.py")
                #saved_path = "none yet"#self.write_to_repo(file_path, code)
                return make_response(
                    sender=self.name,
                    recipient=message["sender"],
                    task_type=task_type,
                    payload={ "code": code}
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

if __name__ == "__main__":
    import sys
    #raw = sys.stdin.read()
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
    "spec":"write for a calculator that can add, subtract, multiply, and divide two numbers. The calculator should have a simple command-line interface."
    
  },
  "status": "pending",
  "error": ""
}
""")
    print("got this far")
    agent = EngineeringAgent()
    response = agent.handle_message(message)
    print(json.dumps(response, indent=2))
