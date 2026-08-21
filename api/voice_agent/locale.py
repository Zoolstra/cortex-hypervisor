"""
Clinic locale resolution.

A clinic's `country` (ISO-3166 alpha-2, on Clinic) and `time_zone` (IANA, on
ClinicLocationDetails) drive: VAPI transcriber language, the locale block
prepended to the system prompt, and "today's local date" for the agent.
"""
import datetime
from zoneinfo import ZoneInfo

from api.core.orm import Clinic


# Country → Deepgram nova-2 language tag (English-speaking markets).
# nova-2 supports `en, en-US, en-AU, en-GB, en-NZ, en-IN` — note the absence
# of `en-CA`. Countries without a region-specific tag fall back to generic `en`.
_TRANSCRIBER_LANGUAGE_BY_COUNTRY: dict[str, str] = {
    "US": "en-US",
    "GB": "en-GB",
    "AU": "en-AU",
    "NZ": "en-NZ",
    "IN": "en-IN",
    # Canada has no en-CA in nova-2; generic `en` is the closest match and
    # transcribes North American English well in practice.
    "CA": "en",
}
_TRANSCRIBER_LANGUAGE_DEFAULT = "en"


def resolve(clinic: Clinic) -> dict:
    """
    Returns the locale config derived from a Clinic ORM (with its
    ClinicLocationDetails relationship loaded).

    Returns:
        {country_code, transcriber_language, timezone, today_local, prompt_block}
    """
    country = (clinic.country or "").upper()
    location = clinic.location
    timezone = (location.time_zone if location else None) or ""

    if not country:
        raise ValueError(f"Clinic {clinic.clinic_id} missing country")
    if not timezone:
        raise ValueError(f"Clinic {clinic.clinic_id} missing time_zone")

    today_local = datetime.datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d")
    transcriber_language = _TRANSCRIBER_LANGUAGE_BY_COUNTRY.get(
        country, _TRANSCRIBER_LANGUAGE_DEFAULT
    )

    # The date in the prompt is a VAPI **template**, not `today_local`.
    #
    # The system prompt is compiled once and pushed to the assistant, so a
    # baked-in date freezes at the moment of the last sync — the live ACNA
    # assistant spent ten days telling the model it was 2026-08-10, which
    # silently skews every relative date the caller offers ("next Tuesday").
    # Nothing was wrong with the value; it was simply computed in the wrong
    # place. VAPI renders the prompt through LiquidJS on each call and fills
    # `now` from the current UTC time, so this resolves at CALL time instead:
    #
    #     {{"now" | date: "%A, %Y-%m-%d", "America/Edmonton"}}
    #        -> "Wednesday, 2026-08-20"
    #
    # The timezone argument is what converts UTC to clinic-local; without it a
    # call after 17:00 MST would read as tomorrow. The weekday is included
    # because the agent has to resolve "next Tuesday" against it, and the ISO
    # date because that is the form the availability/booking tools take.
    #
    # `today_local` is still returned for server-side callers, but it must NOT
    # go back into the prompt — that is precisely the bug this replaced.
    today_template = (
        f'{{{{"now" | date: "%A, %Y-%m-%d", "{timezone}"}}}}'
    )

    prompt_block = (
        "## Locale\n"
        f"- Country: {country}\n"
        f"- Timezone: {timezone}\n"
        f"- Today's date (clinic local): {today_template}\n"
        "Always reason about days, hours, and appointment times in the clinic's local timezone."
    )

    return {
        "country_code": country,
        "transcriber_language": transcriber_language,
        "timezone": timezone,
        "today_local": today_local,
        "prompt_block": prompt_block,
    }
