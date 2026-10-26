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


def test_a_place_before_the_verb_is_still_an_ask():
    said = ("In the app on your phone, open the page I sent (the folded one) and pinch in, then "
            "spread your fingers. How did it feel?")
    assert (by_hand(said) or "").startswith("In the app on your phone")
    assert by_hand("On the Mac, sign in to the browser.")


def test_gestures_are_asks():
    for text in ("Pinch in to fold one level, and spread your fingers to open one.",
                 "Spread your fingers on the page.",
                 "Swipe left on the card.",
                 "Scroll to the end of the page.",
                 "Drag the slider to the right.",
                 "Long-press the icon."):
        assert by_hand(text), text


def test_a_place_with_someone_else_acting_is_not_an_ask():
    for text in ("In the log, the deploy opened a pull request.",
                 "On the phone, the app shows the new level.",
                 "In the test, the page scrolls to the end by itself."):
        assert by_hand(text) is None, text
