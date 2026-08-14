import json
import tempfile
import unittest
from pathlib import Path

import bot


class ProfileRemovalTests(unittest.TestCase):
    def test_only_supported_profiles_are_exposed(self):
        self.assertEqual([key for key, _label in bot.PROFILE_MENU], ["gemma4unc", "chatgptweb"])
        self.assertEqual(bot.normalize_profile("gemma4unc"), "gemma4unc")
        self.assertEqual(bot.normalize_profile("chatgpt_web"), "chatgptweb")
        self.assertEqual(bot.profile_to_model("chatgptweb"), "chatgpt-5.6-sol-high-web")
        self.assertEqual(bot.profile_to_model("chatgpt-5.5-high-web"), "chatgpt-5.6-sol-high-web")
        self.assertEqual(bot.normalize_profile("mythosnano"), "gemma4unc")
        self.assertEqual(bot.profile_to_model("coderq3"), "gemma4unc")

    def test_stale_persisted_state_migrates_without_losing_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = bot.STATE_PATH
            try:
                bot.STATE_PATH = Path(tmp) / "state.json"
                original_state = {
                    "chats": {
                        "123": {
                            "profile": "mythosnano",
                            "model": "mythosnanoq6",
                            "system_prompt": "preserve me",
                            "history": [{"role": "user", "content": "hello"}],
                        },
                        "456": {
                            "profile": "chatgpt_web",
                            "model": bot.CHATGPT_WEB_MODEL,
                            "system_prompt": "external",
                            "history": [],
                        },
                    }
                }
                bot.STATE_PATH.write_text(json.dumps(original_state))
                self.assertEqual(bot.migrate_persisted_state(), 2)
                migrated = json.loads(bot.STATE_PATH.read_text())
                self.assertEqual(migrated["chats"]["123"]["profile"], "gemma4unc")
                self.assertEqual(migrated["chats"]["123"]["model"], "gemma4unc")
                self.assertEqual(migrated["chats"]["123"]["system_prompt"], "preserve me")
                self.assertEqual(migrated["chats"]["123"]["history"], original_state["chats"]["123"]["history"])
                self.assertEqual(migrated["chats"]["456"]["profile"], "chatgptweb")
                self.assertEqual(migrated["chats"]["456"]["model"], bot.CHATGPT_WEB_MODEL)
                self.assertEqual(bot.migrate_persisted_state(), 0)
            finally:
                bot.STATE_PATH = original


if __name__ == "__main__":
    unittest.main()
