"""The drafting instructions: single source of truth for every drafting run.

draft_packet.py writes these rules, enums and worked examples into every
packet file; check_drafts.py validates the output against the same enums
and schema. Neither script has its own copy, so they cannot drift apart.

What the model is asked to do: read a few news articles and copy out
(a) each announcement of MONEY for flood relief, recovery or
reconstruction, and (b) each sign that a named ORGANISATION is working in
a named corridor place. It copies words exactly as written. It never
computes an amount, converts a currency, or decides a pcode: the code does
all of that afterwards, and a person reviews every item before it can be
published.

The worked examples are invented ("Example Relief Trust", "उदाहरण
फाउन्डेसन") so that no real organisation is ever described by text we
made up.
"""

import json

from config import REPO_ROOT

# The id of the model that drafts entries in-session is kept in a private,
# uncommitted one-line file. It is recorded in each packet index and on
# every ledger entry it drafted; METHODS names it.
DRAFT_MODEL_FILE = REPO_ROOT / "draft_model.txt"


def draft_model() -> str:
    """The drafting model's id, read from draft_model.txt."""
    if not DRAFT_MODEL_FILE.exists():
        raise SystemExit(f"{DRAFT_MODEL_FILE.name} is missing: create it with the drafting "
                         "model's id on one line (it is never committed)")
    model = DRAFT_MODEL_FILE.read_text(encoding="utf-8").strip()
    if not model:
        raise SystemExit(f"{DRAFT_MODEL_FILE.name} is empty")
    return model

# Words per verbatim excerpt the model may copy (the public files carry at
# most 25 words; publish_week.py trims and checks that separately).
MAX_EXCERPT_WORDS = 40

STATUS = ["announced", "pledged", "committed", "disbursed"]   # weakest -> firmest
AMOUNT_BASIS = ["total", "per_household", "per_person"]
FORM = ["cash", "in_kind_valued", "mixed", "unspecified"]
DONOR_TYPES = ["govt_federal", "govt_provincial", "govt_local", "foreign_government",
               "un_agency", "multilateral_bank", "red_cross_movement", "international_ngo",
               "national_ngo", "private_sector", "diaspora_individual", "other"]
GEO_LEVELS = ["municipality", "district", "province", "corridor", "national"]
SECTORS = ["shelter_housing", "cash_multipurpose", "food", "wash", "health", "education",
           "roads_bridges", "hydropower_energy", "livelihoods_agriculture", "protection",
           "multi_sector", "unspecified"]
PHASES = ["relief", "recovery", "reconstruction", "unspecified"]
CURRENCY_HINTS = ["NPR", "USD", "AUD", "CAD", "NZD", "EUR", "GBP", "CHF", "CNY", "JPY", "INR",
                  "KRW", "MYR", "DKK", "SEK", "SGD", "HKD", "AED", "SAR", "QAR", "THB", "KWD",
                  "CZK", "NOK", "unknown"]
CONFIDENCE = ["high", "medium", "low"]
ORG_TYPES = ["local_ngo", "national_ngo", "international_ngo", "un_agency",
             "red_cross_movement", "government_fund", "other"]

_ENTRY = {
    "type": "object", "additionalProperties": False,
    "required": ["amount_text", "currency_hint", "amount_basis", "unit_count_text", "status",
                 "form", "donor_as_written", "donor_name_en", "donor_type",
                 "recipient_as_written", "place_as_written", "geo_hint", "sector", "phase",
                 "is_this_flood", "evidence_excerpt", "confidence"],
    "properties": {
        "amount_text": {"type": "string", "minLength": 1},
        "currency_hint": {"enum": CURRENCY_HINTS},
        "amount_basis": {"enum": AMOUNT_BASIS},
        "unit_count_text": {"type": ["string", "null"]},
        "status": {"enum": STATUS},
        "form": {"enum": FORM},
        "donor_as_written": {"type": "string", "minLength": 1},
        "donor_name_en": {"type": "string", "minLength": 1},
        "donor_type": {"enum": DONOR_TYPES},
        "recipient_as_written": {"type": ["string", "null"]},
        "place_as_written": {"type": ["string", "null"]},
        "geo_hint": {"enum": GEO_LEVELS},
        "sector": {"enum": SECTORS},
        "phase": {"enum": PHASES},
        "is_this_flood": {"type": "boolean"},
        "evidence_excerpt": {"type": "string", "minLength": 1},
        "confidence": {"enum": CONFIDENCE},
    },
}
_ORG = {
    "type": "object", "additionalProperties": False,
    "required": ["org_as_written", "org_name_en", "org_type", "place_as_written",
                 "activity", "evidence_excerpt"],
    "properties": {
        "org_as_written": {"type": "string", "minLength": 1},
        "org_name_en": {"type": "string", "minLength": 1},
        "org_type": {"enum": ORG_TYPES},
        "place_as_written": {"type": "string", "minLength": 1},
        "activity": {"type": "string", "minLength": 1, "maxLength": 120},
        "evidence_excerpt": {"type": "string", "minLength": 1},
    },
}
# One output line per article, even when it has nothing to record.
OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["article_id", "entries", "orgs"],
    "properties": {
        "article_id": {"type": "string"},
        "entries": {"type": "array", "items": _ENTRY},
        "orgs": {"type": "array", "items": _ORG},
    },
}

RULES = f"""\
You are a careful research assistant building a public record of money for
the recovery from the 26 August 2026 Rasuwa / Bhote Koshi / Trishuli flood
in Nepal. Each article below is from Nepali or English-language news.

For EACH article, write exactly one output line with two lists:

"entries": every announcement of MONEY for this flood's relief, recovery
or reconstruction. One entry per distinct amount (the same money repeated
in one article is ONE entry). Record:
- amount_text: the amount EXACTLY as written, copied character for
  character (keep Devanagari digits, "Rs", "रु.", "crore", "करोड"...).
  Copy only the amount phrase, e.g. "Rs 2.5 crore", "रु ५ करोड",
  "36 लाख अमेरिकी डलर". Never convert, round or add up numbers.
- currency_hint: the currency the text NAMES. A bare "$" or "dollars"
  with no country is "unknown" - never guess USD. Lakh/crore with no
  currency word is "unknown".
- amount_basis: "total", or "per_household"/"per_person" when the amount
  is given per family/household/person (e.g. "Rs 100,000 to each family").
- unit_count_text: for per-household/per-person amounts, the count EXACTLY
  as written if the text gives one ("300 families", "९७४ परिवार"), else null.
- status, weakest to firmest:
    announced  - says it will give / plans / intends ("will provide",
                 "दिने", "घोषणा")
    pledged    - a formal promise to a named recipient ("pledged",
                 "प्रतिबद्धता")
    committed  - formally decided, approved, allocated or signed
                 ("cabinet decided", "allocated", "agreement signed",
                 "निर्णय गरेको", "विनियोजन")
    disbursed  - actually handed over, released, distributed or spent
                 ("handed over", "released", "distributed", "हस्तान्तरण",
                 "वितरण", "निकासा भइसकेको")
  If unsure between two, choose the weaker one.
- form: cash, in_kind_valued (goods WITH a stated money value), mixed, or
  unspecified.
- donor_as_written: the giver's name EXACTLY as it appears in the article.
  donor_name_en: the same name in English. donor_type: one of
  {", ".join(DONOR_TYPES)}.
- recipient_as_written: who receives it, exactly as written, or null.
- If one amount goes to EACH of several places ("Rs 10 million each to
  Rasuwa and Nuwakot", "रसुवा र नुवाकोटलाई एक/एक करोड"), write a separate
  entry for each place, each with the same amount_text and excerpt. If one
  amount is shared by several places without saying how, write ONE entry
  with place_as_written null and geo_hint corridor. Never divide amounts.
- If an article gives a TOTAL and also lists the parts that make it up
  (e.g. "Rs 3 crore in all: Rs 1 crore each to Rasuwa, Nuwakot and
  Dhading"), record only the parts. Record the total only when the parts
  are not given. Never record both: that counts the same money twice.
- place_as_written: the most specific corridor place the money is FOR,
  exactly as written (e.g. "Gosaikunda", "रसुवा", "Syabrubesi"), or null
  if it is for the flood response in general or for a national fund.
  It is where the money GOES, never where the donor is from: "Syangja
  municipalities donate to the Prime Minister's fund" has no place
  (national), and so does a gift from "a bank in Biratnagar".
  geo_hint: municipality, district, province, corridor (several corridor
  districts or "flood-affected areas"), or national (e.g. the Prime
  Minister's relief fund).
- sector: one of {", ".join(SECTORS)}.
  phase: relief (rescue, food, tents, first weeks), recovery (cash,
  livelihoods, temporary shelter), reconstruction (rebuilding houses,
  roads, bridges, hydropower), or unspecified.
- is_this_flood: true only if the money is for THIS flood. Money for the
  2015 earthquake, another disaster, or ordinary budgets is false.
- evidence_excerpt: one continuous passage (at most {MAX_EXCERPT_WORDS} words) copied
  EXACTLY from the article text that contains the amount_text. Do not fix
  spelling, do not join two separate sentences with "...", do not
  translate. It is checked character by character against the stored
  article; anything not found word for word is rejected.
- confidence: high, medium or low.

NOT entries (leave them out): estimates of damage or losses, insurance
claims, money stolen or recovered by police, fundraising targets that have
not been given, ordinary government budgets, salaries, prices, and
anything about a different disaster.

"orgs": every sign that a NAMED organisation that people could donate to
is WORKING in a named corridor place (distributing aid, running a camp,
rebuilding, giving money there). That means charities, NGOs, community
groups, UN agencies, the Red Cross, and relief funds. NOT companies,
banks, utilities, ministries, the army or police.
Record org_as_written and place_as_written exactly as written, org_name_en,
org_type (one of {", ".join(ORG_TYPES)}), a short activity in English
(under 15 words), and an evidence_excerpt copied exactly from the article.
A municipality's own disaster relief fund counts ("government_fund").

Use ONLY the article text in this file. Do not search the web or use
outside knowledge. If an article has no money and no organisations, its
line has two empty lists.
"""

# Worked examples: invented articles (inputs) and the correct output line.
EXAMPLES = [
    {
        "article": "Kathmandu, Sept 14: The Example Relief Trust has signed an agreement "
                   "with Gosaikunda Rural Municipality to provide Rs 2.5 crore for rebuilding "
                   "50 houses destroyed by the Bhote Koshi flood. The trust's volunteers have "
                   "been distributing tarpaulins in Dhunche since Sunday.",
        "output": {"article_id": "example1", "entries": [{
            "amount_text": "Rs 2.5 crore", "currency_hint": "NPR", "amount_basis": "total",
            "unit_count_text": None, "status": "committed", "form": "cash",
            "donor_as_written": "Example Relief Trust", "donor_name_en": "Example Relief Trust",
            "donor_type": "national_ngo", "recipient_as_written": "Gosaikunda Rural Municipality",
            "place_as_written": "Gosaikunda", "geo_hint": "municipality",
            "sector": "shelter_housing", "phase": "reconstruction", "is_this_flood": True,
            "evidence_excerpt": "The Example Relief Trust has signed an agreement with "
                                "Gosaikunda Rural Municipality to provide Rs 2.5 crore for "
                                "rebuilding 50 houses destroyed by the Bhote Koshi flood.",
            "confidence": "high"}],
            "orgs": [{"org_as_written": "Example Relief Trust",
                      "org_name_en": "Example Relief Trust", "org_type": "national_ngo",
                      "place_as_written": "Dhunche", "activity": "distributing tarpaulins",
                      "evidence_excerpt": "The trust's volunteers have been distributing "
                                          "tarpaulins in Dhunche since Sunday."}]},
    },
    {
        "article": "The Sample Society will give Rs 15,000 to each of 300 flood-affected "
                   "families in Uttargaya next week. Separately, a community group in Sydney "
                   "has sent $4,200 to flood victims in Rasuwa. Officials estimate the flood "
                   "caused losses of Rs 20 billion.",
        "output": {"article_id": "example2", "entries": [
            {"amount_text": "Rs 15,000", "currency_hint": "NPR", "amount_basis": "per_household",
             "unit_count_text": "300 flood-affected families", "status": "announced",
             "form": "cash", "donor_as_written": "Sample Society",
             "donor_name_en": "Sample Society", "donor_type": "national_ngo",
             "recipient_as_written": None, "place_as_written": "Uttargaya",
             "geo_hint": "municipality", "sector": "cash_multipurpose", "phase": "recovery",
             "is_this_flood": True,
             "evidence_excerpt": "The Sample Society will give Rs 15,000 to each of 300 "
                                 "flood-affected families in Uttargaya next week.",
             "confidence": "high"},
            {"amount_text": "$4,200", "currency_hint": "unknown", "amount_basis": "total",
             "unit_count_text": None, "status": "disbursed", "form": "cash",
             "donor_as_written": "a community group in Sydney",
             "donor_name_en": "A community group in Sydney", "donor_type": "diaspora_individual",
             "recipient_as_written": "flood victims in Rasuwa", "place_as_written": "Rasuwa",
             "geo_hint": "district", "sector": "unspecified", "phase": "relief",
             "is_this_flood": True,
             "evidence_excerpt": "a community group in Sydney has sent $4,200 to flood "
                                 "victims in Rasuwa.",
             "confidence": "medium"}],
            "orgs": []},
        "note": "The Rs 20 billion is a loss estimate, not money given: no entry. The bare "
                "'$' is 'unknown' because Sydney suggests Australian dollars.",
    },
    {
        "article": "उदाहरण फाउन्डेसनले नौकुण्ड गाउँपालिकाका बाढी पीडितका लागि रु ५ करोड "
                   "हस्तान्तरण गरेको छ । नमुना बैंकले प्रधानमन्त्री दैवी प्रकोप उद्धार कोषमा "
                   "डेढ करोड रुपैयाँ सहयोग गरेको छ ।",
        "output": {"article_id": "example3", "entries": [
            {"amount_text": "रु ५ करोड", "currency_hint": "NPR", "amount_basis": "total",
             "unit_count_text": None, "status": "disbursed", "form": "cash",
             "donor_as_written": "उदाहरण फाउन्डेसन", "donor_name_en": "Example Foundation",
             "donor_type": "national_ngo", "recipient_as_written": "नौकुण्ड गाउँपालिका",
             "place_as_written": "नौकुण्ड", "geo_hint": "municipality",
             "sector": "unspecified", "phase": "relief", "is_this_flood": True,
             "evidence_excerpt": "उदाहरण फाउन्डेसनले नौकुण्ड गाउँपालिकाका बाढी पीडितका लागि "
                                 "रु ५ करोड हस्तान्तरण गरेको छ ।",
             "confidence": "high"},
            {"amount_text": "डेढ करोड रुपैयाँ", "currency_hint": "NPR", "amount_basis": "total",
             "unit_count_text": None, "status": "disbursed", "form": "cash",
             "donor_as_written": "नमुना बैंक", "donor_name_en": "Sample Bank",
             "donor_type": "private_sector",
             "recipient_as_written": "प्रधानमन्त्री दैवी प्रकोप उद्धार कोष",
             "place_as_written": None, "geo_hint": "national", "sector": "unspecified",
             "phase": "relief", "is_this_flood": True,
             "evidence_excerpt": "नमुना बैंकले प्रधानमन्त्री दैवी प्रकोप उद्धार कोषमा डेढ करोड "
                                 "रुपैयाँ सहयोग गरेको छ ।",
             "confidence": "high"}],
            "orgs": [{"org_as_written": "उदाहरण फाउन्डेसन", "org_name_en": "Example Foundation",
                      "org_type": "national_ngo", "place_as_written": "नौकुण्ड",
                      "activity": "handed over relief money to the municipality",
                      "evidence_excerpt": "उदाहरण फाउन्डेसनले नौकुण्ड गाउँपालिकाका बाढी "
                                          "पीडितका लागि रु ५ करोड हस्तान्तरण गरेको छ ।"}]},
        "note": "Devanagari digits and words are copied exactly. A gift to the Prime "
                "Minister's fund has no place: geo_hint national.",
    },
    {
        "article": "The municipality has allocated Rs 3 crore in its annual budget for "
                   "road upgrades in Kakani. Work on the 2015 earthquake reconstruction "
                   "of the ward office is also complete.",
        "output": {"article_id": "example4", "entries": [], "orgs": []},
        "note": "Ordinary budget and 2015 earthquake work: not this flood, so no entries.",
    },
]


def render_examples() -> str:
    parts = []
    for i, ex in enumerate(EXAMPLES, 1):
        parts.append(f"### Example {i}\nArticle text:\n{ex['article']}\n\nCorrect output line:\n"
                     + json.dumps(ex["output"], ensure_ascii=False)
                     + (f"\n\nWhy: {ex['note']}" if ex.get("note") else "") + "\n")
    return "\n".join(parts)
