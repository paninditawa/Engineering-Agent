{"name": "file_writer_tool", 
"parameters": {"filename": "tests/test_number_guessing_game.py", 
               "content": """
import unittest
from number_guessing_game import NumberGuessingGame

class TestNumberGuessingGame(unittest.TestCase):
    def setUp(self):
        self.game = NumberGuessingGame()

    def test_start_new_game_reset(self):
        self.game.start_new_game()
        self.assertNotEqual(self.game.number_to_guess, random.randint(1, 100))

    def test_make_guess_correct(self):
        result = self.game.make_guess(50)
        self.assertEqual(result, "correct")

    def test_make_guess_incorrect(self):
        result = self.game.make_guess(30)
        self.assertIn("Incorrect", result)

""",
               "directory": None, 
                "overwrite": True}}