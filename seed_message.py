"""
seed_message.py
Run this once to insert a test PM message into the local MongoDB.
The engineering agent will pick it up on its next poll.
"""

from pymongo import MongoClient

MONGO_URI = "mongodb://localhost:27017"
MONGO_DB  = "kanosei"

message = {
    "id": "req-002",
    "timestamp": "2026-04-25T00:00:00Z",
    "sender": "PM",
    "recipient": "ENG",
    "task_type": "IMPLEMENT_FEATURE",
    "context": {
        "priority": "high",
        "target_release": "2026-05-01"
    },
    "payload": {
        "feature_id": "FT-001",
        "feature_name": "Number guessing game",
        "spec_link": "",
        "acceptance_criteria": [
            "Write a Python class that simulates a number guessing game",
            "The game generates a random number between 1 and 100",
            "The player has a limited number of attempts to guess the number",
            "After each guess provide feedback: too high, too low, or correct"
        ]
    },
    "status": "pending",
    "error": ""
}

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
result = db.messages.insert_one(message)
print(f"Inserted message with _id: {result.inserted_id}")
print("The engineering agent will pick this up on its next poll.")
