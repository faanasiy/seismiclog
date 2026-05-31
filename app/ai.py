"""LLM client: OpenRouter -> Anthropic -> local template.

stdlib ``urllib.request`` only.  Each provider returns
``(text, model_id)`` or raises.  ``generate_briefing`` is the entrypoint
the rest of the app uses; it never raises and always returns a tuple.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request


log = logging.getLogger("seismiclog.ai")

TIMEOUT_S = 20.0

SYSTEM_PROMPT = (
    "You are a calm, factual seismic hazard analyst writing for a "
    "non-expert resident. Two short paragraphs. Do not invent numbers."
)


def build_user_prompt(
    address: str,
    region: str,
    n_events_30y: int,
    max_mag: float | None,
    max_mag_date: str,
    depth_band: str,
    soil_class: str,
    p_m5_30y: float,
) -> str:
    """Exact prompt template per spec §4.2."""
    max_mag_str = "n/a" if max_mag is None else f"{max_mag:.1f}"
    return (
        f"Address: {address}\n"
        f"Region: {region}\n"
        f"Last 30 years within 100 km:\n"
        f"  - events M>=4: {n_events_30y}\n"
        f"  - max magnitude observed: {max_mag_str} ({max_mag_date})\n"
        f"  - dominant depth band: {depth_band}\n"
        f"Soil class (Vs30 inferred): {soil_class}\n"
        f"Estimated probability of M>=5 in next 30 years: {p_m5_30y:.1%}\n\n"
        "Write a 2-paragraph seismic risk briefing for the resident.\n"
        "Paragraph 1: what the historical record actually shows for this address.\n"
        "Paragraph 2: practical guidance on what this number means and one\n"
        "preparation action that fits the risk level.\n"
        "Plain English, no jargon, no bullet lists, no emojis."
    )


# ---------- OpenRouter ----------

def _try_openrouter(user_prompt: str) -> tuple[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    model = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://example.invalid/seismiclog",
            "X-Title": "SeismicLog",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    text = payload["choices"][0]["message"]["content"]
    return text.strip(), f"openrouter:{model}"


# ---------- Anthropic ----------

def _try_anthropic(user_prompt: str) -> tuple[str, str]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
    body = {
        "model": model,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
        "max_tokens": 400,
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    text = payload["content"][0]["text"]
    return text.strip(), f"anthropic:{model}"


# ---------- Local template fallback ----------

def _local_template(
    address: str,
    region: str,
    n_events_30y: int,
    max_mag: float | None,
    max_mag_date: str,
    depth_band: str,
    soil_class: str,
    p_m5_30y: float,
) -> tuple[str, str]:
    """Two-paragraph fallback text built with f-strings."""
    if p_m5_30y < 0.10:
        tone = "low"
        action = "secure tall furniture to wall studs"
    elif p_m5_30y < 0.40:
        tone = "moderate"
        action = "keep a 72-hour water and food kit and know where the gas shut-off valve is"
    else:
        tone = "elevated"
        action = (
            "drill drop, cover, hold quarterly with the household and confirm "
            "the building's seismic retrofit status with the owner or municipality"
        )

    max_mag_phrase = (
        f"The largest event recorded within 100 km was magnitude {max_mag:.1f} on {max_mag_date}."
        if max_mag is not None
        else "No magnitude 4 or greater event has been recorded within 100 km in the available record."
    )

    p1 = (
        f"For {address} ({region}), the last 30 years of data show {n_events_30y} events of "
        f"magnitude 4 or greater within 100 kilometres. {max_mag_phrase} The dominant depth band "
        f"for these events is {depth_band}, on {soil_class.lower()}. Together these numbers "
        f"point to a {tone} long-term shaking risk for the address."
    )
    p2 = (
        f"A rough heuristic, based on the local rate of magnitude 4 events, puts the chance of a "
        f"magnitude 5 or greater within 100 km in the next 30 years at about {p_m5_30y*100:.0f} percent. "
        f"This is not an official USGS forecast and a real national hazard model may give a different "
        f"figure. Given the {tone} estimate, a sensible next step for this household is to {action}."
    )
    return f"{p1}\n\n{p2}", "local-template:0.1"


# ---------- Personal prep checklist ----------

CHECKLIST_SYSTEM_PROMPT = (
    "You are a seismic preparedness coach. You produce short, concrete "
    "to-do items for one specific household. You never invent data, you "
    "never give medical advice, and you never reference your own role."
)

BUILDING_TYPES = ("house", "apartment", "office")


def _risk_tier(p_m5_30y: float) -> str:
    if p_m5_30y < 0.10:
        return "low"
    if p_m5_30y < 0.40:
        return "moderate"
    return "elevated"


def build_checklist_prompt(
    address: str,
    building_type: str,
    soil_class: str,
    p_m5_30y: float,
    tier: str,
) -> str:
    return (
        f"Address: {address}\n"
        f"Building type: {building_type}\n"
        f"Soil class (Vs30 inferred): {soil_class}\n"
        f"Probability of M>=5 within 100 km in 30 years: {p_m5_30y:.1%}\n"
        f"Risk tier: {tier}\n"
        "\n"
        "Produce a personal earthquake-prep checklist for this household.\n"
        "Output 6 plain-text lines, one item per line, no numbering, no bullets,\n"
        "no emojis. Each item is one imperative sentence under 20 words and is\n"
        "specifically relevant to this building type, soil class, and risk tier.\n"
        "Cover: structural prep, secure-objects, supplies, drill, document,\n"
        "neighbour or building-owner coordination - in roughly that order."
    )


_CHECKLIST_FALLBACK_GRID: dict[tuple[str, str], list[str]] = {
    ("low", "house"): [
        "Walk the foundation and ground floor once this year to note any new cracks.",
        "Strap the water heater to wall studs with two metal straps.",
        "Keep one week of water and a basic first-aid kit in the garage.",
        "Run a drop-cover-hold drill with the household every six months.",
        "Photograph each room for the home inventory and store it in the cloud.",
        "Exchange phone numbers with two close neighbours for post-event check-ins.",
    ],
    ("moderate", "house"): [
        "Have a contractor inspect the cripple-wall bracing and foundation bolts.",
        "Anchor bookcases, china cabinets, and the TV with stud-mounted straps.",
        "Stock 72 hours of water, food, meds, and cash in a labelled bin.",
        "Quarterly drop-cover-hold drill plus one gas shut-off practice per year.",
        "Scan IDs, insurance, and deed; keep an encrypted copy off-site.",
        "Agree on a neighbourhood meeting point and an out-of-state contact.",
    ],
    ("elevated", "house"): [
        "Engage a structural engineer to confirm seismic retrofit status this year.",
        "Anchor every tall piece of furniture and secure water heater and appliances.",
        "Keep two weeks of water, food, meds, cash, and a manual gas wrench by the exit.",
        "Drill drop-cover-hold quarterly and rehearse one full evacuation route.",
        "Store deed, insurance, and IDs in a go-bag plus an encrypted cloud backup.",
        "Coordinate with neighbours on a buddy-check list and shared supply cache.",
    ],
    ("low", "apartment"): [
        "Ask the property manager for the building's seismic inspection date.",
        "Use museum putty on shelves; strap the TV and any tall furniture.",
        "Keep one week of water and a small kit by the apartment door.",
        "Run a drop-cover-hold drill twice a year with everyone in the unit.",
        "Photograph each room and store the inventory in cloud storage.",
        "Introduce yourself to the neighbour across the hall for mutual check-ins.",
    ],
    ("moderate", "apartment"): [
        "Request the building's most recent seismic retrofit report from the owner.",
        "Anchor bookcases and the TV; clear heavy items from above the bed.",
        "Stock 72 hours of water, food, meds, and a flashlight in a hall closet.",
        "Quarterly drop-cover-hold drills, including under the sturdiest furniture.",
        "Keep IDs, lease, and renters insurance in a go-bag by the front door.",
        "Trade phone numbers with two neighbours and agree on a stairwell meet-up.",
    ],
    ("elevated", "apartment"): [
        "Demand a written seismic retrofit status from the building owner this quarter.",
        "Anchor or remove every tall or heavy object; never sleep under a shelf.",
        "Pre-stage two weeks of water, food, meds, cash, and a hard-hat in a go-bag.",
        "Drill drop-cover-hold quarterly and walk every stair route to the street.",
        "Carry IDs, lease, and renters insurance scans on an encrypted USB plus cloud.",
        "Form a floor-level buddy team and pick a sidewalk meet-up away from glass.",
    ],
    ("low", "office"): [
        "Confirm the office has a posted evacuation plan and a designated warden.",
        "Anchor server racks, filing cabinets, and any tall shelving to wall studs.",
        "Keep a small kit per floor: water, granola bars, flashlight, first-aid.",
        "Run one annual drop-cover-hold drill jointly with the fire drill.",
        "Back up critical documents off-site and keep a printed contact tree.",
        "Designate one warden per floor and rehearse the post-event headcount.",
    ],
    ("moderate", "office"): [
        "Have a structural engineer confirm the office building's retrofit status.",
        "Strap every server rack, bookshelf, and monitor; secure overhead lighting.",
        "Pre-stage 72-hour kits on each floor with water, snacks, meds, and hard hats.",
        "Quarterly drop-cover-hold drill plus one full evacuation per year.",
        "Maintain off-site backups of records and a printed staff contact tree.",
        "Assign two wardens per floor and practice an after-quake roll-call.",
    ],
    ("elevated", "office"): [
        "Commission a recent seismic assessment and post the retrofit status visibly.",
        "Anchor and brace every rack, shelf, monitor arm, and overhead fixture.",
        "Maintain two-week kits on every floor with water, food, meds, and PPE.",
        "Drill drop-cover-hold quarterly and run a full evacuation twice a year.",
        "Replicate critical records to a geographically separate backup site.",
        "Stand up a warden team per floor and rehearse a building-wide muster.",
    ],
}


def _checklist_fallback(building_type: str, tier: str) -> tuple[list[str], str]:
    key = (tier, building_type)
    items = _CHECKLIST_FALLBACK_GRID.get(key)
    if items is None:
        items = _CHECKLIST_FALLBACK_GRID[("moderate", "apartment")]
    return list(items), "local-template:checklist"


def _parse_checklist_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        # Strip leading bullets / numbers that the LLM may include despite the prompt.
        for prefix in ("- ", "* ", "• "):
            if s.startswith(prefix):
                s = s[len(prefix):].strip()
                break
        if len(s) > 2 and s[0].isdigit() and s[1] in ".)":
            s = s[2:].strip()
        if s:
            lines.append(s)
    return lines[:8]


def _checklist_openrouter(prompt: str) -> tuple[str, str] | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    model = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": CHECKLIST_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 350,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://example.invalid/seismiclog",
            "X-Title": "SeismicLog",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"].strip(), f"openrouter:{model}"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError, KeyError, IndexError) as exc:
        log.warning("checklist openrouter failed: %s", exc)
        return None


def _checklist_anthropic(prompt: str) -> tuple[str, str] | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
    body = {
        "model": model,
        "system": CHECKLIST_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 350,
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["content"][0]["text"].strip(), f"anthropic:{model}"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError, KeyError, IndexError) as exc:
        log.warning("checklist anthropic failed: %s", exc)
        return None


def generate_checklist(
    address: str,
    building_type: str,
    soil_class: str,
    p_m5_30y: float,
) -> tuple[list[str], str, str]:
    """Return ``(items, tier, model_id)`` for the prep checklist."""
    bt = (building_type or "").strip().lower()
    if bt not in BUILDING_TYPES:
        bt = "apartment"
    tier = _risk_tier(p_m5_30y)

    if os.environ.get("DEMO_OFFLINE", "0") == "1":
        items, model = _checklist_fallback(bt, tier)
        return items, tier, model

    prompt = build_checklist_prompt(address, bt, soil_class, p_m5_30y, tier)
    for fn in (_checklist_openrouter, _checklist_anthropic):
        result = fn(prompt)
        if result is None:
            continue
        text, model = result
        items = _parse_checklist_lines(text)
        if len(items) >= 4:
            return items, tier, model

    items, model = _checklist_fallback(bt, tier)
    return items, tier, model


def generate_briefing(
    address: str,
    region: str,
    n_events_30y: int,
    max_mag: float | None,
    max_mag_date: str,
    depth_band: str,
    soil_class: str,
    p_m5_30y: float,
) -> tuple[str, str]:
    """Return ``(summary_text, model_id)`` using the provider chain.

    Never raises.  Order: OpenRouter -> Anthropic -> local template.
    The local template always succeeds, so the function is total.
    """
    user_prompt = build_user_prompt(
        address, region, n_events_30y, max_mag, max_mag_date,
        depth_band, soil_class, p_m5_30y,
    )

    if os.environ.get("DEMO_OFFLINE", "0") == "1":
        return _local_template(
            address, region, n_events_30y, max_mag, max_mag_date,
            depth_band, soil_class, p_m5_30y,
        )

    for fn in (_try_openrouter, _try_anthropic):
        try:
            return fn(user_prompt)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError, KeyError, IndexError, RuntimeError) as exc:
            log.warning("ai.provider_failed: %s: %s", fn.__name__, exc)
            continue

    return _local_template(
        address, region, n_events_30y, max_mag, max_mag_date,
        depth_band, soil_class, p_m5_30y,
    )
