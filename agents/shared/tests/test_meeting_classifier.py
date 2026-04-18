"""Shared videoconference detector used by Sergeant Murphy (meetings-coach)
and Mistress Mouse (family-calendar) to split calendar items into the
meeting vs event queues.

The rule (2026-04-18, supersedes the Workflowy-link boundary): an event
with a videoconferencing link is a meeting (Murphy), everything else is
an event (Mouse). Both agents MUST apply the same predicate or the two
queues will overlap or leak.
"""
from __future__ import annotations

from agents.shared.meeting_classifier import has_videoconference_link


def test_hangout_link_counts_as_videoconference():
    assert has_videoconference_link({"hangoutLink": "https://meet.google.com/abc-defg-hij"})


def test_empty_hangout_link_does_not_count():
    assert not has_videoconference_link({"hangoutLink": ""})


def test_conference_data_video_entry_point_counts():
    event = {
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "phone", "uri": "tel:+1-555-1234"},
                {"entryPointType": "video", "uri": "https://zoom.us/j/12345"},
            ]
        }
    }
    assert has_videoconference_link(event)


def test_conference_data_phone_only_does_not_count():
    event = {
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "phone", "uri": "tel:+1-555-1234"},
            ]
        }
    }
    assert not has_videoconference_link(event)


def test_description_zoom_url_counts():
    event = {"description": "Dial in via https://zoom.us/j/987654321 or call us."}
    assert has_videoconference_link(event)


def test_description_meet_url_counts():
    event = {"description": "Join here https://meet.google.com/foo-bar-baz cheers"}
    assert has_videoconference_link(event)


def test_description_teams_url_counts():
    event = {"description": "Click https://teams.microsoft.com/l/meetup-join/... to join"}
    assert has_videoconference_link(event)


def test_description_webex_url_counts():
    event = {"description": "Webex: https://example.webex.com/meet/alice cheers"}
    assert has_videoconference_link(event)


def test_plain_in_person_event_does_not_count():
    event = {
        "summary": "exploring ballet",
        "description": "meet at the studio at 4pm",
        "attendees": [{"email": "sam@example.com"}],
    }
    assert not has_videoconference_link(event)


def test_attendees_alone_do_not_count():
    """2026-04-18 fix: under the new rule an in-person invite with
    co-attendees is an event, not a meeting. Only videoconference
    presence flips the classifier."""
    event = {"attendees": [{"email": "a@b.com"}, {"email": "c@d.com"}]}
    assert not has_videoconference_link(event)


def test_non_dict_is_not_a_meeting():
    assert not has_videoconference_link(None)
    assert not has_videoconference_link("not a dict")
    assert not has_videoconference_link([])


def test_empty_event_is_not_a_meeting():
    assert not has_videoconference_link({})


def test_case_insensitive_description_match():
    event = {"description": "Join at HTTPS://ZOOM.US/J/99 please"}
    assert has_videoconference_link(event)
