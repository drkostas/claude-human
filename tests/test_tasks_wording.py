"""by_hand: a text that tells the person to act by hand is found; a question, a quote and a sentence
where someone else acts are not."""
from claude_human.tasks import HandAsk, by_hand, hand_ask


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


def test_open_as_an_adjective_is_not_an_ask():
    for text in ("Open items, each with its date.",
                 "Open questions for the next round.",
                 "Open pull requests are listed below.",
                 "Open question: which account should the assistant use?"):
        assert by_hand(text) is None, text
    for text in ("Open the items page.",
                 "Open Settings and turn it off.",
                 "Please open your tasks.",
                 "You need to open the question."):
        assert by_hand(text), text


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


def test_hand_ask_names_the_verb_and_keeps_the_quotation():
    found = hand_ask('Press "Decline": nothing needs your Photos.')
    assert found == HandAsk(sentence='Press "Decline": nothing needs your Photos.', verb="Press")
    found = hand_ask("All fine. Please press Decline (Don't Allow): nothing needs your Photos.")
    assert found.verb == "press"
    assert found.sentence == "Please press Decline (Don't Allow): nothing needs your Photos."
    assert by_hand('Press "Decline": nothing needs your Photos.') == 'Press "Decline": nothing needs your Photos.'


def test_a_recommendation_of_a_choice_is_not_an_ask():
    for text in ("Decline on this approval is the right answer.",
                 "Decline is the right answer, nothing needs your Photos."):
        assert hand_ask(text) is None, text


def test_the_verb_is_given_as_written_with_single_spaces():
    assert hand_ask("You need to sign   in to the browser.").verb == "sign in"
    assert hand_ask("In the app on your phone, Open the page.").verb == "Open"
    assert hand_ask("Feed the cat.", verbs=("feed",)).verb == "Feed"


def test_a_quotation_at_the_start_of_the_sentence_found_is_kept():
    said = 'The tool said "done". "Allow" then tap it.'
    assert by_hand(said) == '"Allow" then tap it.'
