"""by_hand: a text that tells the person to act by hand is found; a question, a quote and a sentence
where someone else acts are not."""
from claude_human.tasks import by_hand


def test_instructions_to_act_by_hand_are_found():
    for text in ("Open the settings page, sign in with your password and tap the Finder.",
                 "Please sign in to the browser.",
                 "Can you plug the cable back in?",
                 "1. Go to Settings. 2. Tap Allow.",
                 "The check failed. Then restart the Mac.",
                 "You need to type the code on the TV."):
        assert by_hand(text), text


def test_questions_quotes_and_others_acting_pass():
    for text in ("Which account should the assistant use?",
                 "Should I open a pull request for it?",
                 'The tool refused it: "open the page and tap Allow" is not an ask.',
                 "Do you want the firewall turned on?",
                 "The assistant opened the page and typed nothing.",
                 ""):
        assert by_hand(text) is None, text


def test_the_sentence_found_is_returned_and_the_verbs_can_be_given():
    assert by_hand("All fine. Open the page now.") == "Open the page now."
    assert by_hand("Feed the cat.", verbs=("feed",)) == "Feed the cat."
