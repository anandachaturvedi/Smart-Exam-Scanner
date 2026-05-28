import os
import json
import base64
import hashlib
import math
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from io import BytesIO
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from urllib.request import Request, urlopen
from flask import Flask, jsonify, request
from PIL import Image, ImageOps, UnidentifiedImageError
import pytesseract
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask import send_from_directory
import os

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
SELENIUM_CACHE_DIR = PROJECT_ROOT / ".selenium-cache"
SELENIUM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("SE_CACHE_PATH", str(SELENIUM_CACHE_DIR))

# Load project-wide .env first, then backend/.env (if present) to allow backend-specific overrides.
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(CURRENT_DIR / ".env", override=True)

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - compatibility for environments without the SDK
    OpenAI = None

try:
    from sympy import simplify, symbols
    from sympy.parsing.sympy_parser import (
        convert_xor,
        implicit_multiplication_application,
        parse_expr,
        standard_transformations,
    )
except ImportError:  # pragma: no cover - optional symbolic math support
    simplify = None
    symbols = None
    parse_expr = None
    standard_transformations = ()
    implicit_multiplication_application = None
    convert_xor = None

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - optional PDF support
    PdfReader = None

try:
    import fitz
except ImportError:  # pragma: no cover - optional PDF OCR fallback support
    fitz = None

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - optional HTML parsing support
    BeautifulSoup = None

try:
    import redis
except ImportError:  # pragma: no cover - optional Redis cache support
    redis = None

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options as ChromeOptions
except ImportError:  # pragma: no cover - optional browser automation fallback
    webdriver = None
    By = None
    ChromeOptions = None

app = Flask(__name__)

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)

class ReferenceDocument(db.Model):
    __tablename__ = "reference_documents"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    exam_name = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(120), nullable=False)
    source = db.Column(db.String(120))
    download_url = db.Column(db.Text)
    file_type = db.Column(db.String(30))
    storage_path = db.Column(db.Text)
    extracted_text = db.Column(db.Text)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class UploadedDocument(db.Model):
    __tablename__ = "uploaded_documents"

    id = db.Column(db.Integer, primary_key=True)
    original_filename = db.Column(db.String(300), nullable=False)
    document_type = db.Column(db.String(40), nullable=False, default="unknown")
    mime_type = db.Column(db.String(120))
    file_size = db.Column(db.Integer)
    storage_path = db.Column(db.Text)
    extracted_text = db.Column(db.Text)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class Evaluation(db.Model):
    __tablename__ = "evaluations"

    id = db.Column(db.Integer, primary_key=True)
    exam_name = db.Column(db.String(200))
    subject = db.Column(db.String(120))
    user_text = db.Column(db.Text, nullable=False)
    topper_text = db.Column(db.Text, nullable=False)
    max_marks = db.Column(db.Float, nullable=False, default=5.0)
    earned_marks = db.Column(db.Float)
    percentage = db.Column(db.Float)
    coverage = db.Column(db.Float)
    accuracy = db.Column(db.Float)
    feedback = db.Column(db.Text)
    result_payload = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class AskMessage(db.Model):
    __tablename__ = "ask_messages"

    id = db.Column(db.Integer, primary_key=True)
    question_text = db.Column(db.Text, nullable=False)
    answer_text = db.Column(db.Text)
    user_context = db.Column(db.Text)
    model_used = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class ChatSession(db.Model):
    __tablename__ = "chat_sessions"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(220), nullable=False)
    model_used = db.Column(db.String(120))
    messages = db.Column(db.JSON, nullable=False, default=list)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())

@app.route("/dev/db-test", methods=["POST"])
def dev_db_test():
    document = ReferenceDocument(
        title="Test Reference Document",
        exam_name="CBSE Class 12",
        subject="Physics",
        source="manual_test",
        download_url="https://example.com/test.pdf",
        file_type="PDF",
        extracted_text="This is a test reference document.",
    )

    db.session.add(document)
    db.session.commit()

    return jsonify(
        {
            "id": document.id,
            "title": document.title,
            "message": "Reference document saved successfully",
        }
    )



CBSE_MODEL_ANSWER_URL = "https://www.cbse.gov.in/cbsenew/model-answer.html"
CACHE_TTL_SECONDS = 900
SEARCH_CACHE_TTL_SECONDS = 24 * 60 * 60
_CATALOG_CACHE = {"expires_at": 0, "data": []}
_COMPARE_CACHE = {}
_MEMORY_CACHE = {}
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_CLIENT = None
REFERENCE_DOWNLOAD_DIR = PROJECT_ROOT / "downloads" / "reference_sheets"
SELENIUM_LOCK = threading.Lock()

if redis is not None:
    try:
        REDIS_CLIENT = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=0.4,
            socket_timeout=0.4,
        )
        REDIS_CLIENT.ping()
    except Exception:
        REDIS_CLIENT = None

TESSERACT_CANDIDATE_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


def should_cache_compare_response(response_payload):
    feedback = (response_payload or {}).get("feedback", "")
    return "LLM suggestions are unavailable" not in feedback and "Final marks are fixed" not in feedback


def normalize_cache_part(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def build_cache_key(namespace, *parts):
    normalized_parts = [normalize_cache_part(part) for part in parts]
    raw_key = json.dumps([namespace, *normalized_parts], sort_keys=True)
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return f"smart_exam_scanner:{namespace}:{digest}"


def get_cached_json(cache_key):
    if REDIS_CLIENT is not None:
        try:
            cached = REDIS_CLIENT.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    cached_item = _MEMORY_CACHE.get(cache_key)
    if not cached_item:
        return None

    expires_at, payload = cached_item
    if expires_at <= time.time():
        _MEMORY_CACHE.pop(cache_key, None)
        return None
    return payload


def set_cached_json(cache_key, payload, ttl_seconds=CACHE_TTL_SECONDS):
    if REDIS_CLIENT is not None:
        try:
            REDIS_CLIENT.setex(cache_key, ttl_seconds, json.dumps(payload))
            return
        except Exception:
            pass

    _MEMORY_CACHE[cache_key] = (time.time() + ttl_seconds, payload)

for tesseract_path in TESSERACT_CANDIDATE_PATHS:
    if os.path.exists(tesseract_path):
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
        break

REFERENCE_SHEETS = {
    "cbse class 12 | physics": """Reference sheet for CBSE Class 12 Physics:
- State Newton's laws clearly and in the correct order.
- Define force as rate of change of momentum.
- Write F = ma for constant mass systems.
- Include SI unit of force as newton.
- Add one real-life example such as pushing a trolley or kicking a football.
- End with a short conclusion on how force changes motion.""",
    "cbse class 12 | chemistry": """Reference sheet for CBSE Class 12 Chemistry:
- Define chemical equilibrium in reversible reactions.
- Mention that forward and backward reaction rates become equal.
- State that concentration remains constant at equilibrium.
- Explain Le Chatelier's principle in one clear line.
- Add one example involving Haber process or esterification.""",
    "cbse class 12 | biology": """Reference sheet for CBSE Class 12 Biology:
- Define reproduction as the biological process of producing offspring.
- Differentiate asexual and sexual reproduction.
- Mention genetic variation as an advantage of sexual reproduction.
- Include one plant or animal example.
- End with the role of reproduction in continuity of species.""",
    "jee mains | physics": """Reference sheet for JEE Mains Physics:
- Start with the governing law or definition.
- Write the core formula and define each symbol.
- Show one standard derivation step or condition of use.
- Mention SI units and dimensions where relevant.
- Add a typical application or exam-oriented shortcut.""",
    "neet | biology": """Reference sheet for NEET Biology:
- Give the textbook definition first.
- Present the process or classification in the correct sequence.
- Mention one diagram label or keyword students often miss.
- Include one NCERT-style example or function.
- Close with one high-yield revision point.""",
}

STOPWORDS = {
    "the", "and", "for", "are", "with", "that", "this", "from", "into", "your", "their", "have",
    "has", "had", "was", "were", "will", "would", "could", "should", "about", "which", "what",
    "when", "where", "while", "then", "than", "them", "they", "there", "here", "also", "only",
    "been", "being", "because", "these", "those", "such", "each", "very", "much", "more", "most",
    "some", "many", "over", "under", "same", "different", "answer", "question", "reference",
    "student", "sheet", "model", "page", "pages", "exam", "class", "marks", "write", "written",
    "using", "used", "give", "given", "than", "into", "through", "between", "both", "them",
    "sample", "examination", "paper", "papers", "section", "attempt", "attempted", "following",
    "explain", "describe", "state", "mention", "define", "definition", "detail", "details",
    "style", "average", "topper", "scan", "scanned", "upload", "uploaded", "download",
}

TOPIC_NOISE_TOKENS = STOPWORDS | {
    "according", "based", "briefly", "clearly", "correct", "incorrect", "important", "include",
    "includes", "including", "complete", "proper", "relevant", "suitable", "expected", "main",
    "point", "points", "line", "lines", "word", "words", "text", "content", "solution",
    "solutions", "marking", "scheme", "rubric", "serial", "number", "chapter", "unit",
}

MARK_TOKEN_REQUIREMENTS = [
    (1, 8),
    (2, 15),
    (3, 25),
    (5, 40),
    (10, 80),
    (15, 120),
    (20, 160),
]

TOKEN_MIN_LENGTH_COMPARABILITY = 4
TOKEN_MIN_LENGTH_ANSWER_LENGTH = 3

SUBJECT_TOKEN_ALIASES = {
    "math": "mathematics",
    "maths": "mathematics",
    "eng": "english",
    "hindi": "hindi",
    "sci": "science",
    "phy": "physics",
    "chem": "chemistry",
    "bio": "biology",
    "lit": "literature",
    "lang": "language",
    "std": "standard",
}

SUBJECT_SEARCH_ALIASES = {
    "hindi": ["Hindi", "हिन्दी", "Hindi A", "Hindi B"],
    "marathi": ["Marathi", "मराठी"],
    "english": ["English", "English Language", "English Literature"],
    "math": ["Mathematics", "Maths", "Math"],
    "maths": ["Mathematics", "Maths", "Math"],
    "mathematics": ["Mathematics", "Maths", "Math"],
    "science": ["Science", "General Science"],
    "social science": ["Social Science", "SST", "Social Studies"],
    "history": ["History", "इतिहास"],
    "geography": ["Geography"],
    "reasoning": ["Reasoning", "Reasoning Ability"],
    "general awareness": ["General Awareness", "GK", "General Knowledge"],
}

SUBJECT_NOISE_TOKENS = {
    "and",
    "all",
    "answer",
    "subject",
    "paper",
    "pdf",
    "code",
    "class",
    "cbse",
    "complete",
    "entire",
    "full",
    "key",
    "model",
    "mock",
    "previous",
    "question",
    "sample",
    "sheet",
    "solved",
    "solution",
    "test",
    "whole",
    "year",
}

SEARCH_RESULT_NOISE_TITLES = {
    "about",
    "current affairs",
    "daily tests",
    "mock interview",
    "our centers",
    "our centre",
    "portals",
    "skip to content",
    "contact",
    "home",
    "resources",
    "resources and guides",
}

GENERIC_SEARCH_RESULT_TITLES = {
    "current affairs",
    "daily tests",
    "mock interview",
    "portals",
}

SEARCH_RESULT_INTENT_KEYWORDS = {
    "answer",
    "model",
    "solved",
    "solution",
    "question",
    "paper",
    "mains",
    "optional",
    "test",
    "essay",
    "air",
    "topper",
    "copy",
}

KNOWN_EXAM_BOARDS = {"cbse", "up", "upsc", "icse", "isc"}

TRUSTED_UPSC_DOMAINS = [
    "visionias.in",
    "vajiramandravi.com",
    "drishtiias.com",
    "shankariasparliament.com",
    "nextias.com",
    "forumias.com",
]

UPSC_CURATED_REFERENCES = [
    {
        "subjects": {"history"},
        "title": "UPSC Mains History Previous Year Questions with Model Answers",
        "url": "https://www.dalvoy.com/en/upsc/mains/previous-years/2025/history-paper-ii",
        "source": "Dalvoy",
        "file_type": "WEB",
    },
    {
        "subjects": {"history"},
        "title": "UPSC IAS Mains History Previous Year Papers",
        "url": "https://iasexamportal.com/upsc-mains/papers/history",
        "source": "IAS Exam Portal",
        "file_type": "WEB",
    },
    {
        "subjects": {"history"},
        "title": "UPSC Mains History Optional Question Papers",
        "url": "https://iasexamportal.com/ebook/upsc-mains-history-papers",
        "source": "IAS Exam Portal",
        "file_type": "WEB",
    },
    {
        "subjects": {"history", "general studies", "gs", "gs 1", "gs paper 1"},
        "title": "UPSC Mains Previous Year Question Papers by Year",
        "url": "https://www.drishtiias.com/free-downloads/mains-papers-by-year",
        "source": "Drishti IAS",
        "file_type": "WEB",
    },
    {
        "subjects": {"history", "general studies", "gs", "gs 1", "gs paper 1"},
        "title": "UPSC Mains Previous Year Question Papers PDF",
        "url": "https://compass.rauias.com/upsc-mains/10-years-paper/",
        "source": "Rau's IAS",
        "file_type": "WEB",
    },
]

DOMAIN_SOURCE_LABELS = {
    "visionias.in": "Vision IAS",
    "vajiramandravi.com": "Vajiram & Ravi",
    "drishtiias.com": "Drishti IAS",
    "shankariasparliament.com": "Shankar IAS Parliament",
    "nextias.com": "Next IAS",
    "forumias.com": "ForumIAS",
    "vajiram-prod.s3.ap-south-1.amazonaws.com": "Vajiram & Ravi",
}

DOMAIN_HOST_ALIASES = {
    "vajiramandravi.com": {
        "vajiram-prod.s3.ap-south-1.amazonaws.com",
    },
}

DIRECT_ADAPTER_SEARCH_PATHS = [
    "/?s={query}",
    "/search?q={query}",
    "/search?query={query}",
    "/?q={query}",
]

SEARCH_QUERY_TEMPLATES = [
    "{exam} {subject} question paper pdf",
    "{exam} {subject} previous year question paper pdf",
    "{exam} {subject} previous question paper pdf",
    "{exam} {subject} old question paper pdf",
    "{exam} {subject} past paper pdf",
    "{exam} {subject} practice set pdf",
    "{exam} {subject} mock test pdf",
    "{exam} {subject} sample paper pdf",
    "{exam} {subject} model paper pdf",
    "{exam} {subject} solved paper pdf",
    "{exam} {subject} question paper with solution pdf",
    "{exam} {subject} solved question paper pdf",
    "{exam} {subject} previous year solved paper pdf",
    "{exam} {subject} answer key pdf",
    "{exam} {subject} answer sheet pdf",
    "{exam} {subject} official question paper pdf",
    "{exam} {subject} model question paper pdf",
    "{exam} {subject} model answer sheet pdf",
    "{exam} {subject} model answers",
    "{exam} {subject} syllabus previous year questions pdf",
    "{exam} {subject} mains answer writing",
    "{exam} {subject} topper answer copy",
    "{exam} {subject} air topper copy pdf",
    "{exam} {subject} previous year solved answers",
]

PAPER_SEARCH_KEYWORDS = {
    "answer",
    "answer key",
    "mock",
    "mock test",
    "model",
    "model paper",
    "old paper",
    "paper",
    "past paper",
    "practice",
    "practice set",
    "previous",
    "previous year",
    "pyq",
    "pyqs",
    "question",
    "question paper",
    "sample",
    "sample paper",
    "solution",
    "solved",
}

SCHOOL_BOARD_HINTS = {
    "andhra",
    "assam",
    "bihar",
    "board",
    "class",
    "cbse",
    "chhattisgarh",
    "delhi",
    "goa",
    "gujarat",
    "haryana",
    "himachal",
    "icse",
    "isc",
    "jharkhand",
    "karnataka",
    "kerala",
    "madhya",
    "maharashtra",
    "msbshse",
    "odisha",
    "punjab",
    "rajasthan",
    "ssc",
    "hsc",
    "tamil",
    "telangana",
    "tripura",
    "up",
    "upmsp",
    "uttar",
    "west",
}

EXAM_SEARCH_ALIASES = {
    "afcat": ["AFCAT"],
    "agniveer": ["Agniveer", "Army Agniveer", "Navy Agniveer", "Air Force Agniveer"],
    "air force": ["Indian Air Force", "Air Force", "IAF"],
    "army": ["Indian Army", "Army", "Army GD", "Army Clerk"],
    "banking": ["Banking", "Bank Exam"],
    "bitsat": ["BITSAT"],
    "capf": ["CAPF", "UPSC CAPF"],
    "cat": ["CAT"],
    "cds": ["CDS", "UPSC CDS", "Combined Defence Services"],
    "civil services": ["Civil Services", "UPSC CSE", "IAS"],
    "cuet": ["CUET"],
    "defence": ["Defence Exam", "Defense Exam"],
    "gate": ["GATE"],
    "ibps": ["IBPS", "IBPS PO", "IBPS Clerk", "IBPS RRB"],
    "jee": ["JEE", "JEE Main", "JEE Advanced"],
    "nda": ["NDA", "UPSC NDA", "National Defence Academy"],
    "navy": ["Indian Navy", "Navy", "Navy SSR", "Navy MR", "Navy Agniveer"],
    "neet": ["NEET", "NEET UG"],
    "police": ["Police Constable", "Police SI", "State Police"],
    "railway": ["Railway", "RRB", "RRC", "RRB NTPC", "RRB Group D", "RRB ALP"],
    "railways": ["Railway", "RRB", "RRC", "RRB NTPC", "RRB Group D", "RRB ALP"],
    "rbi": ["RBI", "RBI Grade B", "RBI Assistant"],
    "sbi": ["SBI", "SBI PO", "SBI Clerk"],
    "ssc cgl": ["SSC CGL"],
    "ssc chsl": ["SSC CHSL"],
    "ssc": ["SSC", "SSC CGL", "SSC CHSL", "SSC MTS", "SSC GD"],
    "state pcs": ["State PCS", "PSC", "Public Service Commission"],
    "upsc": ["UPSC", "UPSC CSE", "IAS"],
}

STATE_BOARD_SEARCH_ALIASES = {
    "andhra": ["Andhra Pradesh Board", "BSEAP", "AP SSC", "AP Intermediate"],
    "assam": ["Assam Board", "SEBA", "AHSEC"],
    "bihar": ["Bihar Board", "BSEB"],
    "chhattisgarh": ["Chhattisgarh Board", "CGBSE"],
    "goa": ["Goa Board", "GBSHSE"],
    "gujarat": ["Gujarat Board", "GSEB"],
    "haryana": ["Haryana Board", "HBSE", "BSEH"],
    "himachal": ["Himachal Pradesh Board", "HPBOSE"],
    "jharkhand": ["Jharkhand Board", "JAC"],
    "karnataka": ["Karnataka Board", "KSEAB", "Karnataka SSLC", "Karnataka PUC"],
    "kerala": ["Kerala Board", "KBPE", "Kerala SSLC", "Kerala DHSE"],
    "madhya": ["MP Board", "MPBSE", "Madhya Pradesh Board"],
    "maharashtra": ["Maharashtra State Board", "MSBSHSE", "Maharashtra SSC", "Maharashtra HSC"],
    "odisha": ["Odisha Board", "BSE Odisha", "CHSE Odisha"],
    "punjab": ["Punjab Board", "PSEB"],
    "rajasthan": ["Rajasthan Board", "RBSE", "BSER"],
    "tamil": ["Tamil Nadu Board", "TN Board", "TNDGE", "Tamil Nadu SSLC", "Tamil Nadu HSC"],
    "telangana": ["Telangana Board", "TS SSC", "TS Inter", "BIE Telangana"],
    "tripura": ["Tripura Board", "TBSE"],
    "uttar": ["UP Board", "UPMSP", "Uttar Pradesh Board"],
    "up board": ["UP Board", "UPMSP", "Uttar Pradesh Board"],
    "west bengal": ["West Bengal Board", "WBBSE", "WBCHSE"],
}

UPSC_HOST_HINTS = {
    "drishtiias.com",
    "forumias.com",
    "nextias.com",
    "shankariasparliament.com",
    "visionias.in",
    "vajiramandravi.com",
}

ENABLE_SELENIUM_FALLBACK = os.getenv("ENABLE_SELENIUM_FALLBACK", "true").strip().lower() in {"1", "true", "yes"}
SELENIUM_HEADLESS = os.getenv("SELENIUM_HEADLESS", "true").strip().lower() in {"1", "true", "yes"}
SELENIUM_USER_DATA_DIR = os.getenv(
    "SELENIUM_USER_DATA_DIR",
    str(PROJECT_ROOT / ".selenium-profile"),
)
SELENIUM_TIMEOUT_SECONDS = int(os.getenv("SELENIUM_TIMEOUT_SECONDS", "12"))
SELENIUM_SEARCH_BUDGET_SECONDS = int(os.getenv("SELENIUM_SEARCH_BUDGET_SECONDS", "120"))
SELENIUM_MAX_QUERIES = int(os.getenv("SELENIUM_MAX_QUERIES", "12"))


DEFAULT_LLM_MODEL = os.getenv("LLM_MODEL", os.getenv("OPENAI_MODEL", "llama3.1:8b"))
DEFAULT_LLM_BASE_URL = os.getenv("LLM_BASE_URL", os.getenv("OPENAI_BASE_URL", "http://localhost:11434/v1"))
DEFAULT_LLM_API_KEY = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", "ollama"))


def clamp_score(value):
    return max(0.0, min(1.0, float(value)))


def get_llm_client():
    if OpenAI is None:
        raise RuntimeError("The OpenAI Python SDK is not installed.")

    api_key = DEFAULT_LLM_API_KEY
    base_url = DEFAULT_LLM_BASE_URL
    model = DEFAULT_LLM_MODEL

    if not model:
        raise RuntimeError("No LLM model is configured.")

    if not api_key:
        raise RuntimeError("Set LLM_API_KEY (or OPENAI_API_KEY).")

    if not base_url:
        raise RuntimeError("Set LLM_BASE_URL (or OPENAI_BASE_URL).")

    client = OpenAI(api_key=api_key, base_url=base_url)
    return client, model


def build_llm_eval_prompt(user_text, topper_text):
    return f"""
You are an expert exam evaluator. Compare the student answer to the reference answer.

Return ONLY valid JSON with this schema:
{{
  "comparable": true,
  "coverage": 0.0,
  "accuracy": 0.0,
  "terminology": 0.0,
  "depth": 0.0,
  "strengths": ["..."],
  "missing_points": ["..."],
  "accuracy_issues": ["..."],
  "improvement_suggestions": ["..."],
  "reference_formulas": ["..."],
  "student_formulas": ["..."]
}}

Rules:
- scores are decimals between 0 and 1
- include at most 5 bullets in each list
- comparable=false only if the answers are clearly about different topics/questions
- keep formulas concise like "F=ma", "E=mc^2", "PV=nRT"

Reference answer:
{topper_text}

Student answer:
{user_text}
""".strip()


def build_question_help_prompt(user_text, topper_text, question_text, comparison_context=""):
    has_context = bool(user_text.strip())
    if not has_context:
        return f"""
You are a helpful LLM tutor, study assistant, and exam writing coach.

Answer the student's question directly. If they ask for interview questions, practice questions, explanations, or study help, provide useful content without asking for an uploaded sheet.

Return plain text. Keep it clear, structured, and practical. Use numbered lists or bullets when helpful.

Student question:
{question_text}
""".strip()

    return f"""
You are a helpful LLM tutor, study assistant, and exam writing coach.

Use the uploaded sheet context only when it is relevant. If the student's question is general, answer generally.

Return plain text. Prefer a direct answer first, then useful explanation, examples, or improvement advice.

Uploaded sheet context:
{user_text or "Not provided."}

Student question:
{question_text}
""".strip()


def extract_message_content(message):
    if not message:
        return ""

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            else:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip()

    return str(content or "")


def clean_markdown_for_display(text):
    cleaned = str(text or "")
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
    cleaned = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?!\*)", r"\1", cleaned)
    return cleaned.strip()


def sanitize_chat_history(messages, max_messages=12):
    sanitized = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = normalize_spaces(item.get("content"))
        if role not in {"user", "assistant"} or not content:
            continue
        sanitized.append({"role": role, "content": content})
    return sanitized[-max_messages:]


def build_chat_context_message(exam_name="", subject="", user_text="", reference_text="", comparison_context=""):
    parts = []
    exam_label = " | ".join(part for part in (normalize_spaces(exam_name), normalize_spaces(subject)) if part)
    if exam_label:
        parts.append(f"Current exam context: {exam_label}")
    if normalize_spaces(user_text):
        parts.append(f"Uploaded/student sheet text:\n{str(user_text).strip()[:5000]}")
    if normalize_spaces(reference_text):
        parts.append(f"Reference/topper sheet text:\n{str(reference_text).strip()[:5000]}")
    if normalize_spaces(comparison_context):
        parts.append(f"Comparison summary:\n{str(comparison_context).strip()[:3000]}")
    if not parts:
        return ""
    return "\n\n".join(parts)


def build_chat_system_prompt(mode="auto"):
    mode_name = (mode or "auto").strip().lower()
    mode_line = "Normal assistant mode."
    if mode_name == "interviewer":
        mode_line = (
            "Interviewer mode: run a realistic mock interview. Start with a professional greeting and ask for a short introduction. "
            "After the candidate replies, ask one intelligent question at a time, adapting difficulty and follow-ups to their answers. "
            "Keep it realistic, warm, and concise. Occasionally give brief feedback, but mostly keep the interview moving naturally."
        )

    return f"""
You are EvalAI Chat, a warm study companion, interviewer, and exam-writing coach.

{mode_line}

Rules:
- Answer general questions clearly and conversationally.
- In interviewer mode, do not turn into a generic assistant. Stay in interviewer behavior unless the user asks to stop the interview.
- In interviewer mode, never use placeholder text such as "[Your Name]", "[Candidate Name]", or template fields. Do not pretend the candidate's name is known unless they told you explicitly.
- In interviewer mode, introduce yourself naturally as the interviewer without inventing a personal name.
- If uploaded sheet text, reference sheet text, or comparison feedback is provided, use it only when relevant to the question.
- If the question is about improving marks, explain what to add, what is missing, and how to phrase it better.
- Prefer plain text with a natural conversational tone. Keep formatting light and do not use markdown bold markers.
- Do not invent that you saw context if none was provided.
""".strip()


def create_chat_completion(client, model, messages, temperature=0.2, prefer_json=False):
    if prefer_json:
        try:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            error_text = str(exc).lower()
            unsupported = (
                "response_format" in error_text
                or "json_object" in error_text
                or "unsupported" in error_text
                or "invalid request" in error_text
            )
            if not unsupported:
                raise

    return client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
    )


def parse_json_from_text(raw_text):
    if not raw_text:
        raise RuntimeError("LLM returned an empty response.")

    text = raw_text.strip()
    if text.startswith("{") and text.endswith("}"):
        return json.loads(text)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("LLM response was not valid JSON.")

    return json.loads(text[start : end + 1])


def _to_unit_score(raw_value):
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if value > 1.0:
        value = value / 100.0
    return clamp_score(value)


def _extract_metric_from_raw_text(raw_text, metric_name):
    patterns = [
        rf"{metric_name}\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        rf"{metric_name}\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_text, flags=re.IGNORECASE)
        if match:
            return _to_unit_score(match.group(1))
    return None


def _extract_bullets_from_raw_text(raw_text, labels):
    items = []
    for label in labels:
        pattern = rf"(?:^|\n)\s*(?:[-*]\s*)?{label}\s*[:\-]\s*(.+)"
        matches = re.findall(pattern, raw_text, flags=re.IGNORECASE)
        for item in matches:
            cleaned = re.sub(r"\s+", " ", item).strip(" .-")
            if cleaned:
                items.append(cleaned)

    if not items:
        for line in raw_text.splitlines():
            line_clean = line.strip()
            if line_clean.startswith(("- ", "* ")):
                bullet = line_clean[2:].strip()
                if bullet:
                    items.append(bullet)
            if len(items) >= 5:
                break

    unique = []
    seen = set()
    for item in items:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:5]


def build_heuristic_eval_result(user_text, topper_text):
    user_tokens = extract_meaningful_tokens(user_text)
    topper_tokens = extract_meaningful_tokens(topper_text)
    overlap = user_tokens & topper_tokens

    topper_count = max(len(topper_tokens), 1)
    user_count = max(len(user_tokens), 1)

    comparable, _stats = texts_are_probably_comparable(user_text, topper_text, return_stats=True)
    if not comparable:
        return {
            "comparable": False,
            "coverage": 0.0,
            "accuracy": 0.0,
            "terminology": 0.0,
            "depth": 0.0,
            "strengths": [],
            "missing_points": ["The uploaded answer and reference sheet are not on the same topic."],
            "accuracy_issues": ["No meaningful topic overlap was found, so marks are zero."],
            "improvement_suggestions": ["Upload the matching answer sheet and reference sheet for evaluation."],
            "reference_formulas": [],
            "student_formulas": [],
        }

    coverage = clamp_score(len(overlap) / topper_count)
    lexical_precision = clamp_score(len(overlap) / user_count)
    terminology = clamp_score((coverage + lexical_precision) / 2.0)
    depth = clamp_score(count_meaningful_tokens(user_text) / max(count_meaningful_tokens(topper_text), 1))
    accuracy = clamp_score((0.65 * lexical_precision) + (0.35 * coverage))

    missing_points = []
    if coverage < 0.6:
        missing_points.append("Key expected points are missing or only partially covered.")
    if depth < 0.6:
        missing_points.append("Answer depth is limited compared to the reference.")

    strengths = []
    if coverage >= 0.7:
        strengths.append("Good topic overlap with the reference answer.")
    if terminology >= 0.65:
        strengths.append("Reasonable usage of relevant terminology.")

    return {
        "comparable": True,
        "coverage": coverage,
        "accuracy": accuracy,
        "terminology": terminology,
        "depth": depth,
        "strengths": strengths or ["Attempted to address the topic."],
        "missing_points": missing_points or ["Add more key points from the expected answer scope."],
        "accuracy_issues": [],
        "improvement_suggestions": ["Use more precise terms and add missing core concepts."],
        "reference_formulas": [],
        "student_formulas": [],
    }


def parse_llm_eval_output(raw_output, user_text, topper_text):
    base = build_heuristic_eval_result(user_text, topper_text)

    try:
        parsed = parse_json_from_text(raw_output)
        if isinstance(parsed, dict):
            for metric_name in ("coverage", "accuracy", "terminology", "depth"):
                value = _to_unit_score(parsed.get(metric_name))
                if value is not None:
                    parsed[metric_name] = value
                else:
                    parsed[metric_name] = base[metric_name]

            for list_name in ("strengths", "missing_points", "accuracy_issues", "improvement_suggestions"):
                if not isinstance(parsed.get(list_name), list):
                    parsed[list_name] = base[list_name]
            return parsed
    except Exception:
        pass

    text = raw_output or ""
    result = dict(base)
    for metric_name in ("coverage", "accuracy", "terminology", "depth"):
        found = _extract_metric_from_raw_text(text, metric_name)
        if found is not None:
            result[metric_name] = found

    result["strengths"] = _extract_bullets_from_raw_text(text, ["strength", "strong point"]) or result["strengths"]
    result["missing_points"] = _extract_bullets_from_raw_text(text, ["missing point", "gap"]) or result["missing_points"]
    result["accuracy_issues"] = _extract_bullets_from_raw_text(text, ["accuracy issue", "error"]) or result["accuracy_issues"]
    result["improvement_suggestions"] = _extract_bullets_from_raw_text(
        text, ["improve", "suggestion", "how to score more"]
    ) or result["improvement_suggestions"]
    return result


def normalize_formula_text(formula):
    text = (formula or "").strip()
    subscript_map = str.maketrans("₀₁₂₃₄₅₆₇₈₉₊₋", "0123456789+-")
    superscript_map = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")

    text = text.translate(subscript_map).translate(superscript_map)
    text = text.replace("Ã—", "*").replace("×", "*").replace("Ã·", "/").replace("÷", "/")
    text = text.replace("âˆ’", "-").replace("−", "-").replace("â€“", "-").replace("–", "-")
    text = text.replace("â†’", "->").replace("→", "->").replace("⇌", "<=>").replace("↔", "<=>")
    text = text.replace("^", "**")
    text = re.sub(r"(<=>|<->|⇌|↔|=>|->|→|←|<-)", "=", text)
    text = text.replace(":", "").replace(";", "").replace(",", "")
    text = re.sub(r"[^A-Za-z0-9=\+\-\*/\.\(\)\[\]]", "", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"=+", "=", text)
    return text


def build_formula_variants(formula):
    base = normalize_formula_text(formula)
    if not base:
        return []

    variants = {base, base.upper()}

    # Common OCR confusion in chemistry-style formulas: O (letter) vs 0 (zero)
    variants.add(base.replace("0", "O"))
    variants.add(base.upper().replace("0", "O"))
    variants.add(re.sub(r"(?<=[A-Z])0(?=\d)", "O", base.upper()))
    variants.add(re.sub(r"(?<=\d)0(?=\d)", "O", base.upper()))

    cleaned = [item for item in variants if item]
    # Keep deterministic order: shortest and simplest first.
    return sorted(set(cleaned), key=lambda item: (len(item), item))


def extract_formula_candidates(text):
    if not text:
        return []

    separator_pattern = r"(=|->|=>|→|⇌|↔|<=>)"
    cleaned_lines = [re.sub(r"^\s*[-*•]+\s*", "", line).strip() for line in text.splitlines()]
    cleaned_lines = [line for line in cleaned_lines if line]

    raw_candidates = []
    index = 0
    while index < len(cleaned_lines):
        line = cleaned_lines[index]
        if re.search(separator_pattern, line):
            composed = line
            lookahead = index + 1
            while lookahead < len(cleaned_lines):
                next_line = cleaned_lines[lookahead]
                if (
                    composed.rstrip().endswith(("+", "-", "*", "/", "="))
                    or next_line.startswith("+")
                    or next_line.startswith("=")
                ):
                    composed = f"{composed} {next_line}"
                    lookahead += 1
                    continue
                break

            raw_candidates.append(composed)
            index = lookahead
            continue

        index += 1

    flattened = re.sub(r"\s*\n\s*", " ", text)
    fallback_matches = re.findall(
        r"([A-Za-z0-9\(\)\[\]\+\-\*/\.\s]+(?:=|->|=>|→|⇌|↔|<=>)[A-Za-z0-9\(\)\[\]\+\-\*/\.\s]+)",
        flattened,
    )
    raw_candidates.extend(fallback_matches)

    candidates = []
    for raw_formula in raw_candidates:
        formula = normalize_formula_text(raw_formula)
        if "=" not in formula:
            continue

        lhs, rhs = formula.split("=", 1)
        if not lhs or not rhs:
            continue
        if not re.search(r"[A-Za-z]", f"{lhs}{rhs}"):
            continue
        if 5 <= len(formula) <= 160:
            candidates.append(formula)

    unique = []
    seen = set()
    for item in candidates:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def formula_to_expr(formula):
    if parse_expr is None or symbols is None:
        raise RuntimeError("SymPy is not installed.")

    normalized = normalize_formula_text(formula)
    if not normalized:
        return None

    symbol_names = sorted(set(re.findall(r"[A-Za-z_]\w*", normalized)))
    local_dict = {name: symbols(name) for name in symbol_names}
    local_dict["pi"] = symbols("pi")

    transformations = standard_transformations + (implicit_multiplication_application, convert_xor)

    if "=" in normalized:
        lhs, rhs = normalized.split("=", 1)
        lhs_expr = parse_expr(lhs, local_dict=local_dict, transformations=transformations, evaluate=True)
        rhs_expr = parse_expr(rhs, local_dict=local_dict, transformations=transformations, evaluate=True)
        return lhs_expr - rhs_expr

    return parse_expr(normalized, local_dict=local_dict, transformations=transformations, evaluate=True)


def formulas_are_equivalent(reference_formula, student_formula):
    ref_variants = build_formula_variants(reference_formula)
    student_variants = build_formula_variants(student_formula)

    # Fast path: direct normalized match before symbolic parsing.
    for ref_variant in ref_variants:
        for student_variant in student_variants:
            if ref_variant == student_variant:
                return True

    for ref_variant in ref_variants:
        for student_variant in student_variants:
            try:
                ref_expr = formula_to_expr(ref_variant)
                student_expr = formula_to_expr(student_variant)
            except Exception:
                continue

            if ref_expr is None or student_expr is None:
                continue

            if simplify(ref_expr - student_expr) == 0:
                return True
            if simplify(ref_expr + student_expr) == 0:
                return True

    return False


def verify_formulas(reference_formulas, student_formulas):
    if simplify is None or parse_expr is None:
        return {
            "enabled": False,
            "total": 0,
            "correct": 0,
            "score": None,
            "details": [],
            "message": "SymPy not installed. Install with: pip install sympy",
        }

    cleaned_reference = [normalize_formula_text(item) for item in reference_formulas if normalize_formula_text(item)]
    cleaned_student = [normalize_formula_text(item) for item in student_formulas if normalize_formula_text(item)]

    if not cleaned_reference:
        return {"enabled": True, "total": 0, "correct": 0, "score": None, "details": []}

    details = []
    correct = 0

    for ref_formula in cleaned_reference:
        matched_formula = ""
        is_correct = False

        for student_formula in cleaned_student:
            try:
                if formulas_are_equivalent(ref_formula, student_formula):
                    matched_formula = student_formula
                    is_correct = True
                    break
            except Exception:
                continue

        if is_correct:
            correct += 1

        details.append(
            {
                "reference_formula": ref_formula,
                "matched_student_formula": matched_formula,
                "is_correct": is_correct,
            }
        )

    total = len(cleaned_reference)
    score = correct / total if total else None
    return {"enabled": True, "total": total, "correct": correct, "score": score, "details": details}


def build_feedback_text(result, formula_check):
    bullets = []

    for item in result.get("strengths", []):
        bullets.append(f"- Strength: {item}")
    for item in result.get("missing_points", []):
        bullets.append(f"- Missing point: {item}")
    for item in result.get("accuracy_issues", []):
        bullets.append(f"- Accuracy issue: {item}")
    for item in result.get("improvement_suggestions", []):
        bullets.append(f"- Improve: {item}")

    if formula_check.get("enabled") and formula_check.get("total", 0) > 0:
        bullets.append(
            f"- Formula check: {formula_check['correct']}/{formula_check['total']} formulas mathematically correct."
        )
        for item in formula_check.get("details", []):
            if not item["is_correct"]:
                student_piece = item["matched_student_formula"] or "not found"
                bullets.append(
                    f"- Formula mismatch: expected {item['reference_formula']}, student {student_piece}."
                )

    return "\n".join(bullets[:18]) if bullets else "- The answer was evaluated successfully."


def build_llm_suggestion_prompt(user_text, topper_text, metrics, score, formula_check, question_scores=None):
    question_score_lines = ""
    if question_scores:
        question_score_lines = "\n".join(
            f"- Q{item['question_number']}: {item['earned_marks']}/{item['max_marks']} marks ({item['percentage']}%)"
            for item in question_scores
        )

    formula_summary = "No formulas were checked."
    if formula_check.get("enabled") and formula_check.get("total", 0) > 0:
        formula_summary = f"{formula_check.get('correct', 0)}/{formula_check.get('total', 0)} formulas matched."

    return f"""
You are a strict but helpful exam answer evaluator and writing coach.

The score below is already final and was calculated by fixed local formulas. Do not recalculate, change, challenge, or suggest a different score.

Final score:
- Marks: {score.get('earned_marks')}/{score.get('max_marks')}
- Percentage: {score.get('percentage')}%
- Coverage: {metrics.get('coverage')}%
- Accuracy: {metrics.get('accuracy')}%
- Terminology: {metrics.get('terminology')}%
- Depth: {metrics.get('depth')}%
- Formula check: {formula_summary}

Question-wise marks, if present:
{question_score_lines or "- Single-question evaluation"}

Write feedback only. Return 7 to 10 useful markdown bullets.
Use these labels exactly at the start of bullets where relevant: Strength, Missing point, Accuracy issue, Suggestion.

Make the advice concrete:
- Mention what the student did well.
- Name the important missing concepts from the reference answer.
- Point out vague, incomplete, or incorrect phrasing.
- Give specific lines/points the student should add to score higher.
- Suggest answer structure: definition, key terms, diagram/formula/example, conclusion.
- For multi-question sheets, include the weakest questions first.

Do not give generic advice like "study more" or "write better". Every bullet should help the student edit this answer.

Student answer:
{user_text[:8000]}

Reference answer:
{topper_text[:8000]}
""".strip()


def build_local_advice_feedback(metrics, score, formula_check, question_scores=None):
    bullets = [
        f"- Suggestion: Final marks are fixed at {score.get('earned_marks')}/{score.get('max_marks')} ({score.get('percentage')}%). Use the points below only to improve the answer, not to recalculate the score.",
    ]

    if question_scores:
        weakest = sorted(question_scores, key=lambda item: item.get("percentage", 0))[:3]
        for item in weakest:
            bullets.append(
                f"- Missing point: Q{item['question_number']} is one of the weaker areas at {item['earned_marks']}/{item['max_marks']} marks. Add the core keywords, steps, and examples expected in the reference answer."
            )

    coverage = float(metrics.get("coverage") or 0)
    accuracy = float(metrics.get("accuracy") or 0)
    terminology = float(metrics.get("terminology") or 0)
    depth = float(metrics.get("depth") or 0)

    if coverage < 60:
        bullets.append("- Missing point: Coverage is low, so several expected ideas from the reference answer are absent. Add the main headings and sub-points before adding extra explanation.")
    else:
        bullets.append("- Strength: The answer covers a reasonable part of the expected content.")

    if accuracy < 65:
        bullets.append("- Accuracy issue: Some points do not align closely with the reference answer. Rewrite vague statements into precise factual lines.")
    else:
        bullets.append("- Strength: The answer has acceptable factual alignment with the reference.")

    if terminology < 65:
        bullets.append("- Suggestion: Use more exact textbook terms from the reference answer. Keywords matter because they signal the concept clearly to the evaluator.")

    if depth < 65:
        bullets.append("- Suggestion: Add depth by giving a definition first, then 2-3 supporting points, and then one example, formula, diagram label, or conclusion.")

    if formula_check.get("enabled") and formula_check.get("total", 0) > 0:
        bullets.append(
            f"- Accuracy issue: Formula check matched {formula_check.get('correct', 0)}/{formula_check.get('total', 0)} formulas. Correct the unmatched formulas before improving presentation."
        )

    bullets.append("- Suggestion: Present the final answer in short point-wise format so each scoring point is visible.")
    return "\n".join(bullets[:10])


def generate_llm_suggestions(client, model, user_text, topper_text, metrics, score, formula_check, question_scores=None):
    if client is None or not model:
        return "- Suggestion: LLM suggestions are unavailable because no LLM client is configured."

    response = create_chat_completion(
        client=client,
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You write feedback only. You never change or recalculate marks.",
            },
            {
                "role": "user",
                "content": build_llm_suggestion_prompt(
                    user_text=user_text,
                    topper_text=topper_text,
                    metrics=metrics,
                    score=score,
                    formula_check=formula_check,
                    question_scores=question_scores,
                ),
            },
        ],
        temperature=0.2,
        prefer_json=False,
    )

    feedback = extract_message_content(response.choices[0].message) if response.choices else ""
    if not feedback.strip():
        return "- Suggestion: LLM suggestions are unavailable because the model returned an empty response."
    return feedback.strip()


def extract_meaningful_tokens(text):
    normalized = re.sub(r"[^a-z0-9\s]", " ", (text or "").casefold())
    tokens = [
        token
        for token in normalized.split()
        if len(token) >= TOKEN_MIN_LENGTH_COMPARABILITY and token not in TOPIC_NOISE_TOKENS
    ]
    return set(tokens)


def count_meaningful_tokens(text):
    normalized = re.sub(r"[^a-z0-9\s]", " ", (text or "").casefold())
    return len(
        [
            token
            for token in normalized.split()
            if len(token) >= TOKEN_MIN_LENGTH_ANSWER_LENGTH and token not in STOPWORDS
        ]
    )


def required_meaningful_tokens_for_marks(max_marks):
    marks = float(max_marks)
    anchors = MARK_TOKEN_REQUIREMENTS

    if marks <= anchors[0][0]:
        scale = anchors[0][1] / anchors[0][0]
        return int(math.ceil(max(marks, 0) * scale))

    for index in range(1, len(anchors)):
        left_marks, left_tokens = anchors[index - 1]
        right_marks, right_tokens = anchors[index]

        if marks <= right_marks:
            slope = (right_tokens - left_tokens) / (right_marks - left_marks)
            interpolated = left_tokens + ((marks - left_marks) * slope)
            return int(math.ceil(interpolated))

    # Continue with the last segment's slope for > highest anchor.
    left_marks, left_tokens = anchors[-2]
    right_marks, right_tokens = anchors[-1]
    slope = (right_tokens - left_tokens) / (right_marks - left_marks)
    extrapolated = right_tokens + ((marks - right_marks) * slope)
    return int(math.ceil(extrapolated))


def normalize_max_marks(raw_value, default=5.0):
    try:
        marks = float(raw_value)
    except (TypeError, ValueError):
        return float(default)
    return marks if marks > 0 else float(default)


def get_mark_aware_weights(max_marks):
    marks = normalize_max_marks(max_marks)

    if marks <= 2:
        return {
            "coverage": 0.30,
            "accuracy": 0.45,
            "terminology": 0.20,
            "depth": 0.05,
        }

    if marks <= 5:
        return {
            "coverage": 0.30,
            "accuracy": 0.40,
            "terminology": 0.20,
            "depth": 0.10,
        }

    if marks <= 10:
        return {
            "coverage": 0.30,
            "accuracy": 0.35,
            "terminology": 0.20,
            "depth": 0.15,
        }

    if marks <= 15:
        return {
            "coverage": 0.28,
            "accuracy": 0.35,
            "terminology": 0.17,
            "depth": 0.20,
        }

    return {
        "coverage": 0.25,
        "accuracy": 0.35,
        "terminology": 0.15,
        "depth": 0.25,
    }


def calculate_weighted_overall(metrics, max_marks):
    weights = get_mark_aware_weights(max_marks)
    overall = clamp_score(
        (clamp_score(metrics.get("coverage", 0.0)) * weights["coverage"])
        + (clamp_score(metrics.get("accuracy", 0.0)) * weights["accuracy"])
        + (clamp_score(metrics.get("terminology", 0.0)) * weights["terminology"])
        + (clamp_score(metrics.get("depth", 0.0)) * weights["depth"])
    )
    return overall, weights


QUESTION_HEADER_PATTERN = re.compile(
    r"(?im)^\s*(?:q(?:uestion)?\s*)?(\d{1,2})(?:\s*[.)-]|\s+)",
)


def extract_marks_from_text(text, default=5.0):
    patterns = [
        r"\[\s*(\d+(?:\.\d+)?)\s*(?:mark|marks|m)\s*\]",
        r"\(\s*(\d+(?:\.\d+)?)\s*(?:mark|marks|m)\s*\)",
        r"(?i)(\d+(?:\.\d+)?)\s*(?:mark|marks)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return normalize_max_marks(match.group(1), default=default)
    return float(default)


def split_question_sections(text):
    source = (text or "").strip()
    if not source:
        return []

    matches = list(QUESTION_HEADER_PATTERN.finditer(source))
    if len(matches) < 2:
        return []

    sections = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        section_text = source[start:end].strip()
        if not section_text:
            continue
        sections.append(
            {
                "question_number": match.group(1),
                "text": section_text,
                "max_marks": extract_marks_from_text(section_text),
            }
        )
    return sections


def question_section_similarity(left_text, right_text):
    left_tokens = extract_meaningful_tokens(left_text)
    right_tokens = extract_meaningful_tokens(right_text)
    if not left_tokens or not right_tokens:
        return 0.0

    overlap = left_tokens & right_tokens
    precision = len(overlap) / max(len(left_tokens), 1)
    recall = len(overlap) / max(len(right_tokens), 1)
    if precision + recall == 0:
        return 0.0
    return (2 * precision * recall) / (precision + recall)


def pair_question_sections(user_text, topper_text):
    user_sections = split_question_sections(user_text)
    topper_sections = split_question_sections(topper_text)

    if len(user_sections) < 2 or len(topper_sections) < 2:
        return []

    topper_by_number = {section["question_number"]: section for section in topper_sections}
    pairs = []
    used_topper_indexes = set()

    for index, user_section in enumerate(user_sections):
        numbered_match = topper_by_number.get(user_section["question_number"])
        numbered_index = topper_sections.index(numbered_match) if numbered_match in topper_sections else None
        numbered_score = (
            question_section_similarity(user_section["text"], numbered_match["text"])
            if numbered_match is not None and numbered_index not in used_topper_indexes
            else 0.0
        )

        best_index = None
        best_score = -1.0
        for candidate_index, topper_candidate in enumerate(topper_sections):
            if candidate_index in used_topper_indexes:
                continue
            score = question_section_similarity(user_section["text"], topper_candidate["text"])
            if candidate_index == numbered_index:
                score += 0.05
            if score > best_score:
                best_score = score
                best_index = candidate_index

        if best_index is None:
            continue

        if numbered_match is not None and numbered_index not in used_topper_indexes and numbered_score >= best_score - 0.05:
            topper_section = numbered_match
            used_topper_indexes.add(numbered_index)
        elif best_score >= 0.08:
            topper_section = topper_sections[best_index]
            used_topper_indexes.add(best_index)
        elif numbered_match is not None and numbered_index not in used_topper_indexes:
            topper_section = numbered_match
            used_topper_indexes.add(numbered_index)
        else:
            continue

        max_marks = extract_marks_from_text(
            f"{user_section['text']}\n{topper_section['text']}",
            default=topper_section.get("max_marks") or user_section.get("max_marks") or 5.0,
        )
        pairs.append(
            {
                "question_number": user_section["question_number"],
                "matched_reference_question_number": topper_section["question_number"],
                "user_text": user_section["text"],
                "topper_text": topper_section["text"],
                "max_marks": max_marks,
            }
        )

    return pairs if len(pairs) >= 2 else []


def get_topic_overlap_stats(user_text, topper_text):
    user_tokens = extract_meaningful_tokens(user_text)
    topper_tokens = extract_meaningful_tokens(topper_text)
    overlap = user_tokens & topper_tokens
    smaller_count = min(len(user_tokens), len(topper_tokens))
    larger_count = max(len(user_tokens), len(topper_tokens))

    return {
        "user_token_count": len(user_tokens),
        "topper_token_count": len(topper_tokens),
        "overlap_count": len(overlap),
        "overlap_ratio": len(overlap) / max(smaller_count, 1),
        "broad_overlap_ratio": len(overlap) / max(larger_count, 1),
        "overlap_tokens": sorted(overlap),
    }


def texts_are_probably_comparable(user_text, topper_text, return_stats=False):
    stats = get_topic_overlap_stats(user_text, topper_text)

    if stats["user_token_count"] < 5 or stats["topper_token_count"] < 5:
        comparable = stats["overlap_count"] >= 1
    else:
        comparable = stats["overlap_count"] >= 2 and stats["overlap_ratio"] >= 0.08

    if return_stats:
        return comparable, stats
    return comparable


def build_zero_score_response(user_text, topper_text, max_marks):
    _comparable, stats = texts_are_probably_comparable(user_text, topper_text, return_stats=True)
    marks = normalize_max_marks(max_marks)
    feedback = "\n".join(
        [
            "- Missing point: The answer does not address the expected points from the reference answer.",
            "- Suggestion: Start by reading the exact question and writing a direct definition or opening statement for that topic.",
            "- Suggestion: Add the key textbook terms, formulas, examples, or diagram labels expected for the question.",
            "- Suggestion: Keep each answer point-wise: definition, core concept, explanation, example, and short conclusion.",
        ]
    )
    return {
        "feedback": feedback,
        "metrics": {
            "coverage": 0.0,
            "accuracy": 0.0,
            "terminology": 0.0,
            "depth": 0.0,
            "overall": 0.0,
        },
        "score": {
            "earned_marks": 0.0,
            "max_marks": round(marks, 2),
            "percentage": 0.0,
        },
        "weights": get_mark_aware_weights(marks),
        "scoring_mode": "not_comparable",
        "formula_check": {"enabled": False, "total": 0, "correct": 0, "score": None, "details": []},
        "model_used": "deterministic-local",
        "provider_base_url": DEFAULT_LLM_BASE_URL,
        "topic_overlap": stats,
    }


def is_near_zero_score(metrics_payload):
    coverage = float(metrics_payload.get("coverage") or 0)
    accuracy = float(metrics_payload.get("accuracy") or 0)
    terminology = float(metrics_payload.get("terminology") or 0)
    overall = float(metrics_payload.get("overall") or 0)
    return coverage <= 5 and accuracy <= 15 and terminology <= 15 and overall <= 12


def force_zero_score_response(base_payload, user_text, topper_text, max_marks, scoring_mode):
    zero_payload = build_zero_score_response(user_text, topper_text, max_marks)
    zero_payload["scoring_mode"] = scoring_mode
    zero_payload["question_scores"] = [
        {
            **item,
            "earned_marks": 0.0,
            "percentage": 0.0,
            "metrics": {
                **item.get("metrics", {}),
                "overall": 0.0,
            },
        }
        for item in base_payload.get("question_scores", [])
    ]
    zero_payload["feedback"] = "\n".join(
        [
            "- Missing point: The answer does not directly answer the questions in the reference.",
            "- Suggestion: Rewrite each answer around the exact question asked instead of giving unrelated points.",
            "- Suggestion: For every question, include the main definition, 2-3 scoring keywords, and one relevant example, formula, or diagram label.",
            "- Suggestion: Avoid broad general statements; write precise textbook-style points that match the topic.",
        ]
    )
    return zero_payload


def extract_text_from_pdf(file_stream):
    if PdfReader is None:
        raise RuntimeError("PDF upload is not available because pypdf is not installed.")

    pdf_bytes = file_stream.read() if hasattr(file_stream, "read") else file_stream
    extracted_chunks = []

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        for page in reader.pages:
            extracted_chunks.append(page.extract_text() or "")
    except Exception:
        extracted_chunks = []

    extracted_text = "\n".join(chunk.strip() for chunk in extracted_chunks if chunk.strip())
    if extracted_text.strip():
        return extracted_text

    if fitz is not None:
        fitz_chunks = []
        try:
            pdf_document = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception:
            pdf_document = None

        try:
            if pdf_document is not None:
                for page_index in range(len(pdf_document)):
                    page_text = pdf_document.load_page(page_index).get_text("text") or ""
                    if page_text.strip():
                        fitz_chunks.append(page_text.strip())
        finally:
            if pdf_document is not None:
                pdf_document.close()

        fitz_text = "\n\n".join(fitz_chunks)
        if fitz_text.strip():
            return fitz_text

    return extract_text_from_scanned_pdf(pdf_bytes)


def extract_text_from_scanned_pdf(pdf_bytes):
    if fitz is None:
        return ""

    extracted_chunks = []
    try:
        pdf_document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return ""

    try:
        for page_index in range(min(len(pdf_document), 12)):
            try:
                page = pdf_document.load_page(page_index)
                pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
                image = Image.open(BytesIO(pix.tobytes("png")))
                page_text = ocr_image_with_fallbacks(image)
            except Exception:
                continue
            if page_text.strip():
                extracted_chunks.append(page_text.strip())
    finally:
        pdf_document.close()

    return "\n\n".join(extracted_chunks)


def preprocess_ocr_image(image):
    grayscale = ImageOps.grayscale(image)
    autocontrast = ImageOps.autocontrast(grayscale)

    # Push faint handwriting/background noise toward a cleaner black/white scan.
    thresholded = autocontrast.point(lambda pixel: 255 if pixel > 180 else 0)

    return thresholded


def ocr_image_with_fallbacks(image):
    candidates = [
        ImageOps.autocontrast(ImageOps.grayscale(image)),
        preprocess_ocr_image(image),
    ]
    configs = [
        "--oem 3 --psm 6",
        "--oem 3 --psm 4",
        "--oem 3 --psm 11",
    ]

    best_text = ""
    for candidate in candidates:
        for config in configs:
            try:
                text = pytesseract.image_to_string(candidate, config=config)
            except Exception:
                text = ""
            if len(text.strip()) > len(best_text.strip()):
                best_text = text

    return best_text


def normalize_spaces(value):
    return re.sub(r"\s+", " ", (value or "")).strip()


def parse_class_and_year(section_text):
    normalized = normalize_spaces(section_text)
    class_match = re.search(r"Class\s+(XII|X)\b", normalized, re.IGNORECASE)
    year_match = re.search(r"(20\d{2})", normalized)

    return {
        "class_name": class_match.group(1).upper() if class_match else "",
        "year": year_match.group(1) if year_match else "",
        "section": normalized,
    }


def normalize_subject_tokens(value):
    normalized = re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()
    if not normalized:
        return []

    tokens = []
    for token in normalized.split():
        tokens.append(SUBJECT_TOKEN_ALIASES.get(token, token))
    return tokens


def required_subject_tokens(subject_query):
    tokens = normalize_subject_tokens(subject_query)
    if not tokens:
        return set()

    required = {token for token in tokens if token not in SUBJECT_NOISE_TOKENS}
    return required


def normalize_search_subject(subject_query):
    subject = normalize_spaces(subject_query)
    raw_tokens = normalize_subject_tokens(subject)
    tokens = {token for token in raw_tokens if token not in SUBJECT_NOISE_TOKENS}
    return subject if tokens else ""


def subject_aliases_for_search(subject_query):
    subject = normalize_search_subject(subject_query)
    if not subject:
        return [""]

    normalized = subject.casefold()
    ascii_subject = subject.isascii()
    aliases = [subject]
    for keyword, keyword_aliases in SUBJECT_SEARCH_ALIASES.items():
        if keyword in normalized:
            aliases.extend(keyword_aliases)

    ordered = []
    seen = set()
    for alias in aliases:
        cleaned = normalize_spaces(alias)
        if ascii_subject and not cleaned.isascii():
            continue
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        ordered.append(cleaned)
    return ordered


def subject_matches_query(subject_query, subject_name):
    required_tokens = required_subject_tokens(subject_query)
    if not required_tokens:
        return True

    subject_tokens = set(normalize_subject_tokens(subject_name))
    if not subject_tokens:
        return False

    return required_tokens.issubset(subject_tokens)


def extract_years_from_query(exam_query):
    return set(re.findall(r"\b20\d{2}\b", exam_query or ""))


def extract_classes_from_query(exam_query):
    query = (exam_query or "").casefold()
    classes = set()

    if re.search(r"\b(class\s*12|class\s*xii|12th|xii)\b", query):
        classes.add("XII")

    if re.search(r"\b(class\s*10|class\s*x\b|10th)\b", query):
        classes.add("X")

    return classes


def extract_board_from_query(exam_query):
    query_tokens = set(re.findall(r"[a-z]+", (exam_query or "").casefold()))
    for board in KNOWN_EXAM_BOARDS:
        if board in query_tokens:
            return board.upper()
    return ""


def unwrap_search_result_url(url):
    parsed = urlparse(url or "")
    if not parsed.scheme and not parsed.netloc and parsed.path.startswith("/l/"):
        query_params = parse_qs(parsed.query)
        wrapped_url = query_params.get("uddg", [""])[0]
        if wrapped_url:
            return unquote(wrapped_url)
    query_params = parse_qs(parsed.query)
    for param_name in ("uddg", "u", "url"):
        wrapped_url = query_params.get(param_name, [""])[0]
        if wrapped_url and wrapped_url.startswith(("http://", "https://")):
            return unquote(wrapped_url)
        if wrapped_url and wrapped_url.startswith("a1"):
            encoded = wrapped_url[2:]
            padding = "=" * (-len(encoded) % 4)
            try:
                decoded = base64.urlsafe_b64decode(f"{encoded}{padding}").decode("utf-8", errors="ignore")
            except Exception:
                decoded = ""
            if decoded.startswith(("http://", "https://")):
                return unquote(decoded)
    return url or ""


def normalize_host(url):
    host = urlparse(url or "").netloc.casefold().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def host_matches_allowed_domains(host, allowed_domains):
    if not allowed_domains:
        return True

    for domain in allowed_domains:
        normalized_domain = domain.casefold()
        if host == normalized_domain or host.endswith(f".{normalized_domain}"):
            return True
        alias_hosts = DOMAIN_HOST_ALIASES.get(normalized_domain, set())
        if host in alias_hosts:
            return True
    return False


def infer_file_type_from_url(url):
    lowered = (url or "").casefold()
    if lowered.endswith(".pdf"):
        return "PDF"
    if lowered.endswith(".zip"):
        return "ZIP"
    if lowered.endswith(".txt"):
        return "TXT"
    return "WEB"


def is_importable_download_url(url):
    lowered = (url or "").casefold()
    return lowered.endswith(".pdf") or lowered.endswith(".zip") or lowered.endswith(".txt")


def sanitize_download_filename(value, fallback="reference-sheet"):
    cleaned = normalize_spaces(value or fallback)
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", cleaned)
    cleaned = re.sub(r"\s+", "-", cleaned).strip(".-_")
    return (cleaned or fallback)[:120]


def extension_from_download(download_url, content_type):
    lowered_url = (download_url or "").casefold()
    path_suffix = Path(urlparse(download_url or "").path).suffix.lower()
    if path_suffix in {".pdf", ".zip", ".txt", ".html", ".htm"}:
        return path_suffix
    if "application/pdf" in content_type:
        return ".pdf"
    if "zip" in content_type:
        return ".zip"
    if "text/plain" in content_type:
        return ".txt"
    if "text/html" in content_type or lowered_url.startswith(("http://", "https://")):
        return ".html"
    return ".bin"


def unique_download_path(directory, filename):
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 1000):
        numbered = directory / f"{stem}-{index}{suffix}"
        if not numbered.exists():
            return numbered
    raise RuntimeError("Could not create a unique download filename.")


def exam_aliases_for_search(exam_name):
    normalized = normalize_spaces(exam_name).casefold()
    aliases = {normalize_spaces(exam_name)}

    for keyword, keyword_aliases in STATE_BOARD_SEARCH_ALIASES.items():
        if keyword in normalized:
            aliases.update(keyword_aliases)

    for keyword, keyword_aliases in EXAM_SEARCH_ALIASES.items():
        if re.search(rf"\b{re.escape(keyword)}\b", normalized):
            aliases.update(keyword_aliases)

    if "up board" in normalized or "uttar pradesh" in normalized or "upmsp" in normalized:
        aliases.update({"UP Board", "UPMSP", "Uttar Pradesh Board"})
        if re.search(r"\b(class\s*10|10th)\b", normalized):
            aliases.update({"UP Board Class 10", "UPMSP Class 10", "UP Board High School"})
        if re.search(r"\b(class\s*12|12th)\b", normalized):
            aliases.update(
                {
                    "UP Board Class 12",
                    "UP Board 12th",
                    "UP Board Intermediate",
                    "UPMSP Class 12",
                    "UPMSP 12th",
                    "UPMSP Intermediate",
                    "Uttar Pradesh Board Class 12",
                    "Uttar Pradesh Intermediate",
                }
            )

    if "maharashtra" in normalized or "msbshse" in normalized:
        aliases.update({"Maharashtra State Board", "MSBSHSE"})
        if re.search(r"\b(class\s*10|10th|ssc)\b", normalized):
            aliases.update({"Maharashtra Board SSC", "MSBSHSE SSC", "Maharashtra SSC"})
        if re.search(r"\b(class\s*12|12th|hsc)\b", normalized):
            aliases.update({"Maharashtra Board HSC", "MSBSHSE HSC", "Maharashtra HSC"})

    if "cbse" in normalized:
        aliases.add("CBSE")
    if "icse" in normalized:
        aliases.add("ICSE")
    if "isc" in normalized:
        aliases.add("ISC")

    class_match = re.search(r"\b(class\s*)?(10|12)(?:th)?\b", normalized)
    class_label = f"Class {class_match.group(2)}" if class_match else ""
    expanded_aliases = set(aliases)
    if class_label:
        for alias in aliases:
            if class_label.casefold() not in alias.casefold():
                expanded_aliases.add(f"{alias} {class_label}")

    preferred = [normalize_spaces(exam_name)]
    for keyword, keyword_aliases in EXAM_SEARCH_ALIASES.items():
        if re.search(rf"\b{re.escape(keyword)}\b", normalized):
            preferred.extend(keyword_aliases)
    for keyword, keyword_aliases in STATE_BOARD_SEARCH_ALIASES.items():
        if keyword in normalized:
            preferred.extend(keyword_aliases)

    ordered = []
    for alias in preferred + sorted(expanded_aliases):
        if not alias:
            continue
        key = normalize_spaces(alias).casefold()
        if key in {normalize_spaces(item).casefold() for item in ordered}:
            continue
        ordered.append(alias)
    return ordered


def build_search_queries(exam_name, subject):
    queries = []
    seen = set()
    subject_aliases = subject_aliases_for_search(subject)
    has_subject_filter = bool(normalize_search_subject(subject))

    exact_templates = [
        '"{exam}" "{subject}" question paper pdf',
        '"{exam}" "{subject}" previous year question paper pdf',
        '"{exam}" "{subject}" pyq pdf',
        '"{exam}" "{subject}" model answer',
    ]
    if has_subject_filter:
        for subject_alias in subject_aliases[:2]:
            for template in exact_templates:
                exact_query = normalize_spaces(template.format(exam=exam_name, subject=subject_alias))
                key = exact_query.casefold()
                if exact_query and key not in seen:
                    seen.add(key)
                    queries.append(exact_query)

    priority_templates = [
        "{exam} {subject} filetype:pdf",
        "{exam} {subject} question paper pdf",
        "{exam} {subject} pyq pdf",
        "{exam} {subject} model paper pdf",
        "{exam} {subject} sample paper pdf",
        "{exam} {subject} previous year question paper pdf",
        "{exam} {subject} solved paper pdf",
        "{exam} {subject} answer key pdf",
        "{exam} {subject} official question paper pdf",
        "{exam} {subject} previous year solved paper pdf",
    ]
    exam_aliases = exam_aliases_for_search(exam_name)
    for template in priority_templates:
        for exam_alias in exam_aliases:
            for subject_alias in subject_aliases:
                query = normalize_spaces(template.format(exam=exam_alias, subject=subject_alias))
                key = query.casefold()
                if key in seen:
                    continue
                seen.add(key)
                queries.append(query)
                if len(queries) >= SELENIUM_MAX_QUERIES:
                    return queries

    exam_only_templates = [
        "{exam} previous year question paper pdf",
        "{exam} question paper pdf",
        "{exam} solved paper pdf",
        "{exam} answer key pdf",
        "{exam} model paper pdf",
        "{exam} model question paper pdf",
        "{exam} sample paper pdf",
        "{exam} mock test paper pdf",
    ]
    for exam_alias in exam_aliases_for_search(exam_name):
        for template in exam_only_templates:
            query = normalize_spaces(template.format(exam=exam_alias))
            key = query.casefold()
            if key in seen:
                continue
            seen.add(key)
            queries.append(query)
            if len(queries) >= SELENIUM_MAX_QUERIES:
                return queries

    return queries[:SELENIUM_MAX_QUERIES]


def is_upsc_related_exam(exam_name):
    normalized = normalize_spaces(exam_name).casefold()
    return any(token in normalized for token in ("upsc", "ias", "cse", "civil services"))


def is_school_board_exam(exam_name):
    tokens = set(re.findall(r"[a-z0-9]+", normalize_spaces(exam_name).casefold()))
    return bool(tokens & SCHOOL_BOARD_HINTS)


def is_exam_mismatched_source(url, exam_name):
    host = normalize_host(url)
    if is_upsc_related_exam(exam_name):
        return False
    return is_school_board_exam(exam_name) and any(host == item or host.endswith(f".{item}") for item in UPSC_HOST_HINTS)


def compute_result_relevance(title, url, exam_name, subject):
    title_text = normalize_spaces(title).casefold()
    haystack = f"{title_text} {url}".casefold()
    exam_tokens = {token for token in re.findall(r"[a-z0-9]+", (exam_name or "").casefold()) if len(token) >= 3}
    subject_tokens = required_subject_tokens(subject)
    title_tokens = set(re.findall(r"[a-z0-9]+", title_text))
    subject_title_hits = len(subject_tokens & title_tokens)
    subject_any_hits = sum(1 for token in subject_tokens if token and token in haystack)
    exam_hits = sum(1 for token in exam_tokens if token and token in haystack)

    score = (subject_title_hits * 25) + (subject_any_hits * 6) + (exam_hits * 3)

    for alias in exam_aliases_for_search(exam_name):
        alias_text = normalize_spaces(alias).casefold()
        if alias_text and alias_text in haystack:
            score += 30

    if title_text == normalize_spaces(subject).casefold():
        score += 30
    if subject_tokens and subject_tokens.issubset(title_tokens):
        score += 20

    if ".pdf" in (url or "").casefold():
        score += 8
    if any(keyword in haystack for keyword in ("air", "topper", "copy", "model answer")):
        score += 4
    for keyword in PAPER_SEARCH_KEYWORDS:
        if keyword in haystack:
            score += 8
    if title_text in GENERIC_SEARCH_RESULT_TITLES:
        score -= 40
    if is_exam_mismatched_source(url, exam_name):
        score -= 100

    return score


def resolve_source_label_from_host(host):
    direct = DOMAIN_SOURCE_LABELS.get(host)
    if direct:
        return direct

    for base_domain, aliases in DOMAIN_HOST_ALIASES.items():
        if host in aliases:
            return DOMAIN_SOURCE_LABELS.get(base_domain, host or "WEB")

    return host or "WEB"


def build_reference_result(result_id, title, href, exam_name, fallback_subject, score):
    host = normalize_host(href)
    return {
        "id": result_id,
        "score": score,
        "subject_name": title or fallback_subject,
        "download_url": href,
        "file_type": infer_file_type_from_url(href),
        "file_size": "",
        "class_name": "",
        "year": "",
        "section": normalize_spaces(exam_name),
        "source": resolve_source_label_from_host(host),
        "importable": is_importable_download_url(href),
    }


def is_noise_anchor_title(title):
    normalized = normalize_spaces(title).casefold()
    return normalized in SEARCH_RESULT_NOISE_TITLES


def result_matches_requested_query(title, href, exam_name, subject):
    title_text = normalize_spaces(title)
    if is_noise_anchor_title(title_text):
        return False
    if is_exam_mismatched_source(href, exam_name):
        return False

    haystack = f"{title_text} {href}".casefold()
    exam_alias_match = any(normalize_spaces(alias).casefold() in haystack for alias in exam_aliases_for_search(exam_name))
    has_paper_keyword = any(keyword in haystack for keyword in PAPER_SEARCH_KEYWORDS | SEARCH_RESULT_INTENT_KEYWORDS)

    subject_tokens = required_subject_tokens(subject)
    if subject_tokens and not any(token in haystack for token in subject_tokens) and not (exam_alias_match and has_paper_keyword):
        return False

    title_tokens = set(re.findall(r"[a-z0-9]+", title_text.casefold()))
    exact_subject_title = title_text.casefold() == normalize_spaces(subject).casefold()
    title_contains_subject = bool(subject_tokens & title_tokens)

    if title_text.casefold() in GENERIC_SEARCH_RESULT_TITLES and not (subject_tokens & title_tokens):
        return False

    exam_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", (exam_name or "").casefold())
        if len(token) >= 3 and token not in {"class", "exam", "board"}
    }
    has_exam_token = any(token in haystack for token in exam_tokens)
    if normalize_spaces(exam_name) and not exam_alias_match and not has_exam_token:
        return False

    if exam_tokens and not exact_subject_title and not title_contains_subject and not exam_alias_match and not any(token in haystack for token in exam_tokens):
        return False

    if not exact_subject_title and not title_contains_subject and not has_paper_keyword:
        return False

    return True


def extract_candidate_links_from_html(html, base_url):
    soup = BeautifulSoup(html, "html.parser")

    anchors = []
    anchors.extend(soup.select("a.result__a"))
    anchors.extend(soup.select("h2.result__title a"))
    anchors.extend(soup.select("main a[href], article a[href], .content a[href], .post a[href], body a[href]"))

    candidates = []
    for anchor in anchors:
        href = unwrap_search_result_url(anchor.get("href", ""))
        if not href:
            continue
        absolute_href = urljoin(base_url, href)
        title = normalize_spaces(anchor.get_text(" ", strip=True))
        candidates.append((title, absolute_href))

    return candidates


def search_domain_with_adapter(domain, exam_name, subject, limit=6):
    if BeautifulSoup is None:
        raise RuntimeError("HTML parsing support is unavailable because beautifulsoup4 is not installed.")

    queries = build_search_queries(exam_name, subject)[:3]
    results = []
    seen_urls = set()

    for query in queries:
        query_encoded = quote_plus(query)
        for path in DIRECT_ADAPTER_SEARCH_PATHS:
            search_url = f"https://{domain}{path.format(query=query_encoded)}"
            request = Request(
                search_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )

            try:
                with urlopen(request, timeout=15) as response:
                    html = response.read()
            except Exception:
                continue

            candidates = extract_candidate_links_from_html(html, search_url)
            for title, href in candidates:
                if not href.startswith("http"):
                    continue

                host = normalize_host(href)
                if not host_matches_allowed_domains(host, [domain]):
                    continue

                if not result_matches_requested_query(title, href, exam_name, subject):
                    continue

                if href in seen_urls:
                    continue

                relevance = compute_result_relevance(title, href, exam_name, subject)
                if relevance < 2:
                    continue

                seen_urls.add(href)
                result = build_reference_result(
                    result_id=f"adapter-{len(results) + 1}",
                    title=title,
                    href=href,
                    exam_name=exam_name,
                    fallback_subject=subject,
                    score=max(1, 120 - (len(results) * 4) + relevance),
                )
                results.append(result)

                if len(results) >= limit:
                    return results

    return results


def search_web_reference_sheets(exam_name, subject, limit=12, allowed_domains=None):
    if BeautifulSoup is None:
        raise RuntimeError("HTML search support is unavailable because beautifulsoup4 is not installed.")

    query_templates = SEARCH_QUERY_TEMPLATES

    if allowed_domains:
        queries = []
        for domain in allowed_domains:
            for template in query_templates:
                queries.append(f"site:{domain} {template.format(exam=exam_name, subject=subject)}")
    else:
        queries = [template.format(exam=exam_name, subject=subject) for template in query_templates]

    results = []
    seen_urls = set()

    for query in queries:
        search_url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        request = Request(
            search_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        with urlopen(request, timeout=20) as response:
            html = response.read()

        soup = BeautifulSoup(html, "html.parser")

        # DuckDuckGo markup can vary; support multiple result title selectors.
        result_anchors = []
        result_anchors.extend(soup.select("a.result__a"))
        result_anchors.extend(soup.select("h2.result__title a"))

        for anchor in result_anchors:
            title = normalize_spaces(anchor.get_text(" ", strip=True))
            href = unwrap_search_result_url(anchor.get("href", ""))
            if not href.startswith("http"):
                continue

            host = normalize_host(href)
            if not host_matches_allowed_domains(host, allowed_domains):
                continue

            if not result_matches_requested_query(title, href, exam_name, subject):
                continue

            if href in seen_urls:
                continue
            seen_urls.add(href)

            source_label = DOMAIN_SOURCE_LABELS.get(host, host or "WEB")
            results.append(
                {
                    "id": f"web-{len(results) + 1}",
                    "score": max(1, 100 - (len(results) * 3)),
                    "subject_name": title or subject,
                    "download_url": href,
                    "file_type": infer_file_type_from_url(href),
                    "file_size": "",
                    "class_name": "",
                    "year": "",
                    "section": normalize_spaces(exam_name),
                    "source": source_label,
                    "importable": is_importable_download_url(href),
                }
            )

            if len(results) >= limit:
                return results

    return results


def start_chrome_driver(options, attempts=2):
    last_error = None
    for attempt in range(attempts):
        try:
            return webdriver.Chrome(options=options)
        except Exception as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise last_error


def search_reference_sheets_with_selenium(exam_name, subject, limit=12, allowed_domains=None):
    if not ENABLE_SELENIUM_FALLBACK:
        return []
    if webdriver is None or ChromeOptions is None:
        return []

    options = ChromeOptions()
    options.page_load_strategy = "eager"
    if SELENIUM_HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--disable-extensions")
    options.add_argument("--lang=en-IN")
    options.add_argument("--window-size=1366,900")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--remote-debugging-port=0")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = None
    SELENIUM_LOCK.acquire()
    try:
        driver = start_chrome_driver(options)
        driver.set_page_load_timeout(SELENIUM_TIMEOUT_SECONDS)
        driver.implicitly_wait(0.5)
        started_at = time.time()
        queries = build_search_queries(exam_name, subject)
        results = []
        seen_urls = set()
        max_candidates = max(limit, 12)
        blocked_pages = 0

        search_pages = []
        for query in queries:
            encoded_query = quote_plus(query)
            search_pages.append(
                (
                    f"https://www.bing.com/search?setlang=en-IN&cc=IN&mkt=en-IN&q={encoded_query}",
                    "li.b_algo h2 a, .b_algo a[href], h2 a",
                )
            )
        for query in queries[: max(4, SELENIUM_MAX_QUERIES // 2)]:
            encoded_query = quote_plus(query)
            search_pages.append(
                (
                    f"https://duckduckgo.com/html/?q={encoded_query}",
                    "a.result__a, h2.result__title a, a[data-testid='result-title-a']",
                )
            )

        for search_url, selector in search_pages:
            if time.time() - started_at > SELENIUM_SEARCH_BUDGET_SECONDS:
                break
            try:
                driver.get(search_url)
            except Exception:
                continue
            time.sleep(0.6)

            try:
                page_text = normalize_spaces(driver.find_element(By.TAG_NAME, "body").text).casefold()
            except Exception:
                page_text = ""
            if any(
                marker in page_text
                for marker in (
                    "one last step",
                    "solve the challenge",
                    "unusual traffic",
                    "checking the proxy",
                    "sorry, but your computer or network may be sending automated queries",
                )
            ):
                blocked_pages += 1
                continue

            selector_anchors = driver.find_elements(By.CSS_SELECTOR, selector)
            fallback_anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
            anchors = selector_anchors + fallback_anchors
            page_seen_urls = set()
            for anchor in anchors:
                try:
                    href = anchor.get_attribute("href") or ""
                    href = urljoin(search_url, href)
                    title = normalize_spaces(
                        anchor.text
                        or anchor.get_attribute("aria-label")
                        or anchor.get_attribute("title")
                        or anchor.get_attribute("innerText")
                        or ""
                    )
                    if not title:
                        title = normalize_spaces(
                            driver.execute_script(
                                "return arguments[0].closest('li, article, div')?.innerText || '';",
                                anchor,
                            )
                        )
                except Exception:
                    continue
                href = unwrap_search_result_url(href)
                if not href.startswith("http"):
                    continue
                if href in page_seen_urls:
                    continue
                page_seen_urls.add(href)

                host = normalize_host(href)
                if host in {"bing.com", "duckduckgo.com"} or host.endswith(".bing.com") or host.endswith(".duckduckgo.com"):
                    continue
                if not host_matches_allowed_domains(host, allowed_domains):
                    continue

                if not result_matches_requested_query(title, href, exam_name, subject):
                    continue

                if href in seen_urls:
                    continue

                seen_urls.add(href)
                relevance = compute_result_relevance(title, href, exam_name, subject)
                if relevance < 2:
                    continue

                results.append(
                    build_reference_result(
                        result_id=f"selenium-{len(results) + 1}",
                        title=title,
                        href=href,
                        exam_name=exam_name,
                        fallback_subject=subject,
                        score=max(1, relevance),
                    )
                )

                if len(results) >= max_candidates:
                    break

            if len(results) >= max_candidates:
                break

        subject_tokens = required_subject_tokens(subject)

        def subject_sort_key(item):
            title_tokens = set(re.findall(r"[a-z0-9]+", normalize_spaces(item.get("subject_name")).casefold()))
            subject_hits = len(subject_tokens & title_tokens)
            exact_subject = normalize_spaces(item.get("subject_name")).casefold() == normalize_spaces(subject).casefold()
            return (
                -int(exact_subject),
                -subject_hits,
                -item.get("score", 0),
                item.get("subject_name", ""),
            )

        results.sort(key=subject_sort_key)
        return results[:limit]
    except Exception as exc:
        app.logger.exception("Selenium reference search failed")
        raise RuntimeError(f"Selenium reference search failed: {exc}") from exc
    finally:
        if driver is not None:
            driver.quit()
        SELENIUM_LOCK.release()


QUESTION_BANK_SECTIONS = [
    {"key": "notes", "title": "Subject-wise notes"},
    {"key": "pyqs", "title": "PYQs"},
    {"key": "books", "title": "Books"},
    {"key": "solved-books", "title": "Solved books"},
]

QUESTION_BANK_CATEGORY_TERMS = {
    "notes": ["notes pdf", "study material pdf", "revision notes pdf"],
    "pyqs": ["previous year question paper pdf", "pyq pdf", "question papers pdf"],
    "books": ["book pdf", "textbook pdf", "guide book pdf"],
    "solved-books": ["solved book pdf", "solutions pdf", "solved papers pdf"],
}


def expand_question_bank_query(query):
    expanded = normalize_spaces(query)
    expanded = re.sub(r"\bclass\s*(9|10|11|12)\b", r"class \1", expanded, flags=re.IGNORECASE)
    expanded = re.sub(r"\b(9|10|11|12)th\b", r"class \1", expanded, flags=re.IGNORECASE)

    stream_aliases = {
        "pcm": "physics chemistry mathematics",
        "pcb": "physics chemistry biology",
        "pcmb": "physics chemistry mathematics biology",
        "commerce": "accountancy business studies economics mathematics",
        "arts": "humanities history geography political science economics sociology",
        "humanities": "history geography political science economics sociology",
        "defence": "nda cds afcat capf ssb defence exam",
    }
    lowered = expanded.casefold()
    additions = [value for key, value in stream_aliases.items() if re.search(rf"\b{re.escape(key)}\b", lowered)]
    if additions:
        expanded = normalize_spaces(f"{expanded} {' '.join(additions)}")
    return expanded


def build_question_bank_queries(query, subject, category_key):
    base_query = expand_question_bank_query(query)
    subject_label = "" if not subject or subject.casefold() == "all" else normalize_spaces(subject)
    search_base = normalize_spaces(f"{base_query} {subject_label}")
    if not search_base:
        return []

    queries = []
    for term in QUESTION_BANK_CATEGORY_TERMS.get(category_key, []):
        queries.append(normalize_spaces(f"{search_base} {term}"))
        queries.append(normalize_spaces(f"{search_base} {term} filetype:pdf"))

    seen = set()
    ordered = []
    for item in queries:
        key = item.casefold()
        if key and key not in seen:
            seen.add(key)
            ordered.append(item)
    return ordered[:3]


def search_question_bank_with_selenium(query, subject, limit_per_section=5):
    sections = [{**section, "items": []} for section in QUESTION_BANK_SECTIONS]
    if not normalize_spaces(query):
        return sections
    if not ENABLE_SELENIUM_FALLBACK or webdriver is None or ChromeOptions is None:
        return sections

    options = ChromeOptions()
    options.page_load_strategy = "eager"
    if SELENIUM_HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--disable-extensions")
    options.add_argument("--lang=en-IN")
    options.add_argument("--window-size=1366,900")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--remote-debugging-port=0")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = None
    SELENIUM_LOCK.acquire()
    try:
        driver = start_chrome_driver(options)
        driver.set_page_load_timeout(SELENIUM_TIMEOUT_SECONDS)
        driver.implicitly_wait(0.5)
        started_at = time.time()

        for section in sections:
            seen_urls = set()
            for search_query in build_question_bank_queries(query, subject, section["key"]):
                if time.time() - started_at > SELENIUM_SEARCH_BUDGET_SECONDS:
                    return sections

                encoded_query = quote_plus(search_query)
                search_pages = [
                    (
                        f"https://www.bing.com/search?setlang=en-IN&cc=IN&mkt=en-IN&q={encoded_query}",
                        "li.b_algo h2 a, .b_algo a[href], h2 a",
                    ),
                    (
                        f"https://duckduckgo.com/html/?q={encoded_query}",
                        "a.result__a, h2.result__title a, a[data-testid='result-title-a']",
                    ),
                ]

                for search_url, selector in search_pages:
                    if len(section["items"]) >= limit_per_section:
                        break
                    try:
                        driver.get(search_url)
                    except Exception:
                        continue
                    time.sleep(0.5)

                    anchors = driver.find_elements(By.CSS_SELECTOR, selector) + driver.find_elements(By.CSS_SELECTOR, "a[href]")
                    for anchor in anchors:
                        try:
                            href = unwrap_search_result_url(urljoin(search_url, anchor.get_attribute("href") or ""))
                        except Exception:
                            continue
                        if not href.startswith("http"):
                            continue

                        host = normalize_host(href)
                        if host in {"bing.com", "duckduckgo.com"} or host.endswith(".bing.com") or host.endswith(".duckduckgo.com"):
                            continue
                        if href in seen_urls:
                            continue

                        try:
                            title = normalize_spaces(
                                anchor.text
                                or anchor.get_attribute("aria-label")
                                or anchor.get_attribute("title")
                                or anchor.get_attribute("innerText")
                                or ""
                            )
                        except Exception:
                            continue
                        if not title or title.casefold() in SEARCH_RESULT_NOISE_TITLES:
                            continue

                        seen_urls.add(href)
                        section["items"].append(
                            {
                                "id": f'{section["key"]}-{len(section["items"]) + 1}',
                                "title": title,
                                "subject": subject if subject and subject.casefold() != "all" else "Web",
                                "meta": f"{DOMAIN_SOURCE_LABELS.get(host, host or 'WEB')} | {infer_file_type_from_url(href)}",
                                "download_url": href,
                            }
                        )

                        if len(section["items"]) >= limit_per_section:
                            break

                if len(section["items"]) >= limit_per_section:
                    break

        return sections
    except Exception as exc:
        app.logger.exception("Selenium question bank search failed")
        raise RuntimeError(f"Selenium question bank search failed: {exc}") from exc
    finally:
        if driver is not None:
            driver.quit()
        SELENIUM_LOCK.release()


def extract_text_from_html_bytes(file_bytes):
    if BeautifulSoup is None:
        raise RuntimeError("HTML parsing is unavailable because beautifulsoup4 is not installed.")

    soup = BeautifulSoup(file_bytes, "html.parser")
    for tag_name in ("script", "style", "noscript", "nav", "header", "footer"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    text = normalize_spaces(soup.get_text("\n", strip=True))
    if len(text) < 80:
        raise RuntimeError("No readable content found on the downloaded page.")

    return text


def fetch_cbse_model_answer_catalog():
    if BeautifulSoup is None:
        raise RuntimeError("HTML search support is unavailable because beautifulsoup4 is not installed.")

    if _CATALOG_CACHE["data"] and _CATALOG_CACHE["expires_at"] > time.time():
        return _CATALOG_CACHE["data"]

    request = Request(
        CBSE_MODEL_ANSWER_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    with urlopen(request, timeout=20) as response:
        html = response.read()

    soup = BeautifulSoup(html, "html.parser")
    catalog = []

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if "model-answer/" not in href or not href.lower().endswith(".zip"):
            continue

        row = anchor.find_parent("tr")
        if row is None:
            continue

        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        subject_name = normalize_spaces(cells[0].get_text(" ", strip=True))
        file_size = normalize_spaces(cells[3].get_text(" ", strip=True))
        section_header = row.find_previous(string=re.compile(r"Model Answer by Candidate for Class", re.IGNORECASE))
        section_info = parse_class_and_year(section_header or "")

        catalog.append(
            {
                "subject_name": subject_name,
                "download_url": urljoin(CBSE_MODEL_ANSWER_URL, href),
                "file_type": "ZIP",
                "file_size": file_size,
                "class_name": section_info["class_name"],
                "year": section_info["year"],
                "section": section_info["section"],
                "source": "CBSE",
            }
        )

    _CATALOG_CACHE["data"] = catalog
    _CATALOG_CACHE["expires_at"] = time.time() + CACHE_TTL_SECONDS

    return catalog


def score_reference_match(entry, exam_query, subject_query):
    score = 0
    exam_query_lower = exam_query.casefold()
    subject_query_lower = subject_query.casefold()
    subject_name_lower = entry["subject_name"].casefold()
    section_lower = entry["section"].casefold()

    if subject_query_lower and subject_query_lower in subject_name_lower:
        score += 6

    subject_tokens = {token for token in re.split(r"[^a-z0-9]+", subject_query_lower) if token}
    subject_name_tokens = {token for token in re.split(r"[^a-z0-9]+", subject_name_lower) if token}
    score += len(subject_tokens & subject_name_tokens) * 2

    if entry["year"] and entry["year"] in exam_query_lower:
        score += 3

    if entry["class_name"] == "XII" and ("12" in exam_query_lower or "xii" in exam_query_lower):
        score += 3
    if entry["class_name"] == "X" and ("10" in exam_query_lower or re.search(r"\bclass x\b", exam_query_lower)):
        score += 3

    if "cbse" in exam_query_lower:
        score += 1

    if exam_query_lower and exam_query_lower in section_lower:
        score += 4

    return score


def extract_text_from_zip_bytes(file_bytes):
    import zipfile

    with zipfile.ZipFile(BytesIO(file_bytes)) as archive:
        pdf_members = [name for name in archive.namelist() if name.lower().endswith(".pdf")]
        text_members = [name for name in archive.namelist() if name.lower().endswith(".txt")]

        pdf_members.sort(key=lambda name: ("model" not in name.casefold(), "answer" not in name.casefold(), len(name)))

        if pdf_members:
            with archive.open(pdf_members[0]) as pdf_file:
                return extract_text_from_pdf(BytesIO(pdf_file.read()))

        if text_members:
            with archive.open(text_members[0]) as text_file:
                return text_file.read().decode("utf-8", errors="ignore")

    raise RuntimeError("Downloaded archive does not contain a supported PDF or text file.")


def find_reference_sheet(exam_details):
    lookup_key = exam_details.casefold().strip()
    direct_match = REFERENCE_SHEETS.get(lookup_key)
    if direct_match:
        return direct_match

    query_tokens = {token for token in lookup_key.replace("|", " ").split() if token}
    if not query_tokens:
        return None

    best_score = 0
    best_match = None

    for key, value in REFERENCE_SHEETS.items():
        key_tokens = {token for token in key.replace("|", " ").split() if token}
        score = len(query_tokens & key_tokens)

        if score > best_score:
            best_score = score
            best_match = value

    return best_match if best_score >= 2 else None


def find_best_resource_download_link(title, subject, meta=""):
    query_exam = normalize_spaces(f"{title} {meta}")
    query_subject = normalize_spaces(subject or "resource")
    candidates = search_reference_sheets_with_selenium(
        query_exam,
        query_subject,
        limit=20,
        allowed_domains=None,
    )

    importable = [item for item in candidates if item.get("importable")]
    if not importable:
        return None

    importable.sort(key=lambda item: (-item.get("score", 0), item.get("subject_name", "")))
    return importable[0]


def search_cbse_reference_catalog(exam_name, subject):
    catalog = fetch_cbse_model_answer_catalog()
    requested_years = extract_years_from_query(exam_name)
    requested_classes = extract_classes_from_query(exam_name)

    ranked_results = []
    for entry in catalog:
        if requested_classes and entry.get("class_name") not in requested_classes:
            continue
        if requested_years and entry.get("year") not in requested_years:
            continue
        if not subject_matches_query(subject, entry["subject_name"]):
            continue

        score = score_reference_match(entry, exam_name, subject)
        if score <= 0:
            continue

        ranked_results.append(
            {
                "id": f'{entry["year"]}-{entry["class_name"]}-{entry["subject_name"]}'.lower().replace(" ", "-"),
                "score": score,
                "importable": True,
                **entry,
            }
        )

    ranked_results.sort(key=lambda item: (-item["score"], item["year"], item["subject_name"]))
    return ranked_results[:10]


def is_up_board_query(exam_name):
    normalized = normalize_spaces(exam_name).casefold()
    return any(
        token in normalized
        for token in ("up board", "upmsp", "uttar pradesh board", "uttar pradesh madhyamik")
    )


def infer_upmsp_subject_from_url(href):
    filename = unquote(Path(urlparse(href).path).name)
    stem = re.sub(r"\.[a-z0-9]+$", "", filename, flags=re.IGNORECASE)
    stem = re.sub(r"^\d+\s*[-_ ]*", "", stem)
    stem = re.sub(r"[_-]?[eh]$", "", stem, flags=re.IGNORECASE)
    stem = stem.replace("_", " ").replace("-", " ")
    return normalize_spaces(stem).title()


def infer_upmsp_class_from_url(href):
    match = re.search(r"Class\s*(\d+)", href, flags=re.IGNORECASE)
    if not match:
        return ""
    return {"10": "X", "12": "XII"}.get(match.group(1), match.group(1))


def search_upmsp_reference_catalog(exam_name, subject):
    if BeautifulSoup is None:
        raise RuntimeError("HTML parsing support is unavailable because beautifulsoup4 is not installed.")

    catalog_url = "https://prereg.upmsp.edu.in/ModelPaper.html"
    request = Request(
        catalog_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urlopen(request, timeout=20) as response:
        html = response.read()

    soup = BeautifulSoup(html, "html.parser")
    requested_classes = extract_classes_from_query(exam_name)
    results = []
    seen_urls = set()

    for anchor in soup.select("a[href]"):
        href = urljoin(catalog_url, anchor.get("href", ""))
        if "/Downloads/ModalPaperClass" not in href:
            continue

        class_name = infer_upmsp_class_from_url(href)
        if requested_classes and class_name not in requested_classes:
            continue

        subject_name = infer_upmsp_subject_from_url(href)
        if not subject_matches_query(subject, subject_name):
            continue

        if href in seen_urls:
            continue
        seen_urls.add(href)

        title = normalize_spaces(f"UPMSP Class {class_name or ''} {subject_name} Model Paper")
        results.append(
            {
                "id": f"upmsp-{len(results) + 1}",
                "score": 250 - len(results),
                "subject_name": title,
                "download_url": href,
                "file_type": infer_file_type_from_url(href),
                "file_size": "",
                "class_name": class_name,
                "year": "2024-25",
                "section": normalize_spaces(exam_name),
                "source": "UPMSP",
                "importable": is_importable_download_url(href),
            }
        )

    return results[:10]


def is_upsc_query(exam_name):
    normalized = normalize_spaces(exam_name).casefold()
    return any(token in normalized for token in ("upsc", "ias", "civil services", "cse"))


def search_upsc_curated_reference_catalog(exam_name, subject):
    if not is_upsc_query(exam_name):
        return []

    subject_tokens = set(required_subject_tokens(subject))
    normalized_subject = normalize_spaces(subject).casefold()
    results = []

    for entry in UPSC_CURATED_REFERENCES:
        entry_subjects = {normalize_spaces(item).casefold() for item in entry["subjects"]}
        entry_tokens = set()
        for item in entry_subjects:
            entry_tokens.update(re.findall(r"[a-z0-9]+", item))

        subject_match = (
            not subject_tokens
            or normalized_subject in entry_subjects
            or bool(subject_tokens & entry_tokens)
            or any(token in normalize_spaces(entry["title"]).casefold() for token in subject_tokens)
        )
        if not subject_match:
            continue

        results.append(
            {
                "id": f"upsc-curated-{len(results) + 1}",
                "score": 240 - len(results),
                "subject_name": entry["title"],
                "download_url": entry["url"],
                "file_type": entry["file_type"],
                "file_size": "",
                "class_name": "",
                "year": "",
                "section": normalize_spaces(exam_name),
                "source": entry["source"],
                "importable": True,
            }
        )

    return results[:10]


@app.route("/resource-download-link", methods=["POST"])
def resource_download_link():
    data = request.get_json(silent=True) or {}
    title = normalize_spaces(data.get("title"))
    subject = normalize_spaces(data.get("subject"))
    meta = normalize_spaces(data.get("meta"))

    if not title:
        return jsonify({"error": "title is required"}), 400

    try:
        best_match = find_best_resource_download_link(title=title, subject=subject, meta=meta)
    except Exception as exc:
        return jsonify({"error": f"Resource search failed: {exc}"}), 500

    if not best_match:
        return jsonify({"error": "No direct downloadable file found for this resource."}), 404

    return jsonify(
        {
            "download_url": best_match.get("download_url", ""),
            "source": best_match.get("source", ""),
            "title": best_match.get("subject_name", title),
            "file_type": best_match.get("file_type", ""),
        }
    )


@app.route("/question-bank-search", methods=["POST"])
def question_bank_search():
    data = request.get_json(silent=True) or {}
    query = normalize_spaces(data.get("query"))
    subject = normalize_spaces(data.get("subject") or "All")

    if not query:
        return jsonify({"sections": [{**section, "items": []} for section in QUESTION_BANK_SECTIONS]})

    cache_key = build_cache_key("question_bank_search", query, subject)
    cached_payload = get_cached_json(cache_key)
    if cached_payload is not None:
        return jsonify({**cached_payload, "cache_hit": True})

    try:
        sections = search_question_bank_with_selenium(query=query, subject=subject, limit_per_section=5)
    except Exception as exc:
        app.logger.exception("Question bank Selenium search route failed")
        return jsonify({"error": f"Selenium search failed: {exc}", "sections": [{**section, "items": []} for section in QUESTION_BANK_SECTIONS]}), 500

    response_payload = {"sections": sections, "cache_hit": False}
    set_cached_json(cache_key, response_payload, ttl_seconds=SEARCH_CACHE_TTL_SECONDS)
    return jsonify(response_payload)




@app.route("/search-reference-sheets", methods=["POST"])
def search_reference_sheets():
    data = request.get_json(silent=True) or {}
    exam_name = normalize_spaces(data.get("exam_name"))
    subject = normalize_spaces(data.get("subject"))

    if not exam_name or not subject:
        return jsonify({"error": "Both exam_name and subject are required"}), 400

    cache_key = build_cache_key("reference_sheet_search", exam_name, subject)
    cached_payload = get_cached_json(cache_key)
    if cached_payload is not None:
        return jsonify({**cached_payload, "cache_hit": True})

    try:
        selenium_results = search_reference_sheets_with_selenium(
            exam_name,
            subject,
            limit=20,
            allowed_domains=None,
        )
    except Exception as exc:
        app.logger.exception("Reference sheet Selenium search route failed")
        return jsonify({"error": f"Selenium search failed: {exc}", "results": [], "top_match": None}), 500

    if not selenium_results:
        return jsonify(
            {
                "error": (
                    "Selenium search did not find a matching resource. "
                    "Check that Chrome/Selenium is installed, or try a more specific exam, board, class, and subject."
                ),
                "results": [],
                "top_match": None,
                "search_engine": "selenium_web",
                "cache_hit": False,
            }
        )

    top_match = next((item for item in selenium_results if item.get("importable")), None)
    response_payload = {
        "results": selenium_results,
        "top_match": top_match or selenium_results[0],
        "search_engine": "selenium_web",
        "cache_hit": False,
    }
    set_cached_json(cache_key, response_payload, ttl_seconds=SEARCH_CACHE_TTL_SECONDS)
    return jsonify(response_payload)


@app.route("/import-reference-sheet", methods=["POST"])
def import_reference_sheet():
    data = request.get_json(silent=True) or {}
    download_url = normalize_spaces(data.get("download_url"))

    if not download_url:
        return jsonify({"error": "download_url is required"}), 400

    try:
        download_request = Request(
            download_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        with urlopen(download_request, timeout=30) as response:
            file_bytes = response.read()
            content_type = (response.headers.get("Content-Type") or "").casefold()

        lowered_url = download_url.casefold()
        if lowered_url.endswith(".zip"):
            extracted_text = extract_text_from_zip_bytes(file_bytes)
        elif lowered_url.endswith(".pdf"):
            extracted_text = extract_text_from_pdf(BytesIO(file_bytes))
        elif lowered_url.endswith(".txt"):
            extracted_text = file_bytes.decode("utf-8", errors="ignore")
        elif "text/html" in content_type or b"<html" in file_bytes[:2048].lower():
            extracted_text = extract_text_from_html_bytes(file_bytes)
        else:
            raise RuntimeError("Unsupported downloaded file type.")
    except Exception as exc:
        return jsonify({"error": f"Import failed: {exc}"}), 500

    if not extracted_text.strip():
        return jsonify({"error": "No readable text was found in the downloaded reference sheet"}), 400

    return jsonify({"extracted_text": extracted_text.strip()})


@app.route("/download-reference-sheet", methods=["POST"])
def download_reference_sheet():
    data = request.get_json(silent=True) or {}
    download_url = normalize_spaces(data.get("download_url"))
    title = normalize_spaces(data.get("title"))

    if not download_url:
        return jsonify({"error": "download_url is required"}), 400
    if not is_importable_download_url(download_url):
        return jsonify(
            {
                "error": (
                    "This result is a web page, not a direct downloadable file. "
                    "Open the source page and download the document there, or upload the file manually."
                )
            }
        ), 400

    try:
        download_request = Request(
            download_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        with urlopen(download_request, timeout=30) as response:
            file_bytes = response.read()
            content_type = (response.headers.get("Content-Type") or "").casefold()

        if not file_bytes:
            return jsonify({"error": "The selected URL returned an empty file."}), 400

        REFERENCE_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        extension = extension_from_download(download_url, content_type)
        base_name = sanitize_download_filename(title or Path(urlparse(download_url).path).stem)
        save_path = unique_download_path(REFERENCE_DOWNLOAD_DIR, f"{base_name}{extension}")
        save_path.write_bytes(file_bytes)
    except Exception as exc:
        return jsonify({"error": f"Download failed: {exc}"}), 500

    return jsonify(
        {
            "message": "Reference sheet downloaded",
            "saved_path": str(save_path),
            "file_name": save_path.name,
            "file_size": len(file_bytes),
        }
    )


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    original_filename = file.filename
    filename = original_filename.lower()
    document_type = normalize_spaces(request.form.get("document_type") or "unknown") or "unknown"

    try:
        file_bytes = file.read()
        file_size = len(file_bytes)
        file_stream = BytesIO(file_bytes)
        if filename.endswith(".pdf"):
            extracted_text = extract_text_from_pdf(file_stream)
        else:
            image = Image.open(file_stream)
            extracted_text = ocr_image_with_fallbacks(image)
    except UnidentifiedImageError:
        return jsonify({"error": "Uploaded file must be a valid JPG, PNG, or PDF"}), 400
    except Exception as exc:
        return jsonify({"error": f"Text extraction failed: {exc}"}), 500

    if not extracted_text.strip():
        return jsonify({"error": "No readable text was found in the uploaded file"}), 400

    uploaded_document = UploadedDocument(
        original_filename=original_filename,
        document_type=document_type,
        mime_type=file.mimetype,
        file_size=file_size,
        extracted_text=extracted_text.strip(),
    )
    db.session.add(uploaded_document)
    db.session.commit()

    return jsonify({"document_id": uploaded_document.id, "extracted_text": extracted_text.strip()})


@app.route("/fetch-topper-sheet", methods=["POST"])
def fetch_topper_sheet():
    data = request.get_json(silent=True) or {}
    exam_details = (data.get("exam_details") or "").strip()

    if not exam_details:
        return jsonify({"error": "Exam details are required"}), 400

    topper_text = find_reference_sheet(exam_details)

    if not topper_text:
        return jsonify({"error": "Reference sheet not found"}), 404

    return jsonify({"topper_text": topper_text})


def evaluate_answer_pair(client, model, user_text, topper_text, max_marks):
    stable_result = build_heuristic_eval_result(user_text, topper_text)

    reference_formulas = extract_formula_candidates(topper_text)
    student_formulas = extract_formula_candidates(user_text)

    formula_check = verify_formulas(reference_formulas, student_formulas)

    coverage = clamp_score(stable_result.get("coverage", 0.0))
    base_accuracy = clamp_score(stable_result.get("accuracy", 0.0))
    terminology = clamp_score(stable_result.get("terminology", 0.0))
    depth = clamp_score(stable_result.get("depth", 0.0))

    overall, weights = calculate_weighted_overall(
        {
            "coverage": coverage,
            "accuracy": base_accuracy,
            "terminology": terminology,
            "depth": depth,
        },
        max_marks,
    )
    earned_marks = overall * normalize_max_marks(max_marks)

    return {
        "formula_check": formula_check,
        "weights": weights,
        "metrics": {
            "coverage": coverage,
            "accuracy": base_accuracy,
            "terminology": terminology,
            "depth": depth,
            "overall": overall,
        },
        "score": {
            "earned_marks": earned_marks,
            "max_marks": normalize_max_marks(max_marks),
            "percentage": overall * 100,
        },
    }


def build_compare_cache_key(user_text, topper_text, max_marks):
    payload = {
        "user_text": normalize_spaces(user_text),
        "topper_text": normalize_spaces(topper_text),
        "max_marks": normalize_max_marks(max_marks),
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def save_evaluation_result(user_text, topper_text, max_marks, response_payload, exam_name="", subject=""):
    score = response_payload.get("score") or {}
    metrics = response_payload.get("metrics") or {}
    evaluation = Evaluation(
        exam_name=normalize_spaces(exam_name),
        subject=normalize_spaces(subject),
        user_text=user_text,
        topper_text=topper_text,
        max_marks=float(score.get("max_marks") or max_marks or 5.0),
        earned_marks=score.get("earned_marks"),
        percentage=score.get("percentage"),
        coverage=metrics.get("coverage"),
        accuracy=metrics.get("accuracy"),
        feedback=response_payload.get("feedback"),
        result_payload=response_payload,
    )
    db.session.add(evaluation)
    db.session.commit()
    response_payload["evaluation_id"] = evaluation.id
    return response_payload


@app.route("/compare", methods=["POST"])
def compare():
    data = request.get_json(silent=True) or {}
    user_text = (data.get("user_text") or "").strip()
    topper_text = (data.get("topper_text") or "").strip()
    exam_name = normalize_spaces(data.get("exam_name"))
    subject = normalize_spaces(data.get("subject"))
    raw_max_marks = data.get("max_marks")

    if not user_text or not topper_text:
        return jsonify({"error": "Both user_text and topper_text are required"}), 400

    max_marks = 5.0
    if raw_max_marks not in (None, ""):
        try:
            max_marks = normalize_max_marks(raw_max_marks)
        except (TypeError, ValueError):
            return jsonify({"error": "max_marks must be a valid number"}), 400

    cache_key = build_compare_cache_key(user_text, topper_text, max_marks)
    if cache_key in _COMPARE_CACHE:
        return jsonify(_COMPARE_CACHE[cache_key])

    question_pairs = pair_question_sections(user_text, topper_text)

    if not texts_are_probably_comparable(user_text, topper_text):
        response_payload = build_zero_score_response(user_text, topper_text, max_marks)
        response_payload = save_evaluation_result(user_text, topper_text, max_marks, response_payload, exam_name, subject)
        _COMPARE_CACHE[cache_key] = response_payload
        return jsonify(response_payload)

    if not question_pairs:
        student_token_count = count_meaningful_tokens(user_text)
        required_tokens = required_meaningful_tokens_for_marks(max_marks)

        if student_token_count < required_tokens:
            return (
                jsonify(
                    {
                        "error": (
                            f"For a {max_marks:g}-mark question, more content is needed."
                        )
                    }
                ),
                400,
            )

    try:
        try:
            client, model = get_llm_client()
        except Exception:
            client = None
            model = DEFAULT_LLM_MODEL or "llm-unavailable"

        if question_pairs:
            evaluations = []
            total_possible = 0.0
            total_earned = 0.0

            for pair in question_pairs:
                evaluation = evaluate_answer_pair(
                    client,
                    model,
                    pair["user_text"],
                    pair["topper_text"],
                    pair["max_marks"],
                )
                total_possible += evaluation["score"]["max_marks"]
                total_earned += evaluation["score"]["earned_marks"]
                evaluations.append(
                    {
                        "question_number": pair["question_number"],
                        "matched_reference_question_number": pair.get("matched_reference_question_number"),
                        "max_marks": round(evaluation["score"]["max_marks"], 2),
                        "earned_marks": round(evaluation["score"]["earned_marks"], 2),
                        "percentage": round(evaluation["score"]["percentage"], 2),
                        "weights": evaluation["weights"],
                        "metrics": {
                            key: round(value * 100, 2)
                            for key, value in evaluation["metrics"].items()
                        },
                    }
                )

            overall = clamp_score(total_earned / total_possible if total_possible else 0.0)

            def weighted_metric(metric_name):
                return sum(
                    item["metrics"][metric_name] * item["max_marks"]
                    for item in evaluations
                ) / max(total_possible, 1)

            formula_check = {"enabled": False, "total": 0, "correct": 0, "score": None, "details": []}
            metrics_payload = {
                "coverage": round(weighted_metric("coverage"), 2),
                "accuracy": round(weighted_metric("accuracy"), 2),
                "terminology": round(weighted_metric("terminology"), 2),
                "depth": round(weighted_metric("depth"), 2),
                "overall": round(overall * 100, 2),
            }
            score_payload = {
                "earned_marks": round(total_earned, 2),
                "max_marks": round(total_possible, 2),
                "percentage": round(overall * 100, 2),
            }
            try:
                feedback = generate_llm_suggestions(
                    client,
                    model,
                    user_text,
                    topper_text,
                    metrics_payload,
                    score_payload,
                    formula_check,
                    question_scores=evaluations,
                )
            except Exception as exc:
                feedback = build_local_advice_feedback(
                    metrics_payload,
                    score_payload,
                    formula_check,
                    question_scores=evaluations,
                )

            response_payload = {
                "feedback": feedback,
                "metrics": metrics_payload,
                "score": score_payload,
                "question_scores": evaluations,
                "scoring_mode": "multi_question",
                "formula_check": formula_check,
                "model_used": model,
                "provider_base_url": DEFAULT_LLM_BASE_URL,
            }
            if is_near_zero_score(metrics_payload):
                response_payload = force_zero_score_response(
                    response_payload,
                    user_text,
                    topper_text,
                    total_possible or max_marks,
                    "multi_question_near_zero_overlap",
                )
            if should_cache_compare_response(response_payload):
                _COMPARE_CACHE[cache_key] = response_payload
            response_payload = save_evaluation_result(
                user_text,
                topper_text,
                total_possible or max_marks,
                response_payload,
                exam_name,
                subject,
            )
            return jsonify(response_payload)

        evaluation = evaluate_answer_pair(client, model, user_text, topper_text, max_marks)
        metrics = evaluation["metrics"]
        score = evaluation["score"]
        metrics_payload = {
            "coverage": round(metrics["coverage"] * 100, 2),
            "accuracy": round(metrics["accuracy"] * 100, 2),
            "terminology": round(metrics["terminology"] * 100, 2),
            "depth": round(metrics["depth"] * 100, 2),
            "overall": round(metrics["overall"] * 100, 2),
        }
        score_payload = {
            "earned_marks": round(score["earned_marks"], 2),
            "max_marks": round(score["max_marks"], 2),
            "percentage": round(score["percentage"], 2),
        }
        try:
            feedback = generate_llm_suggestions(
                client,
                model,
                user_text,
                topper_text,
                metrics_payload,
                score_payload,
                evaluation["formula_check"],
            )
        except Exception as exc:
            feedback = build_local_advice_feedback(
                metrics_payload,
                score_payload,
                evaluation["formula_check"],
            )

        response_payload = {
            "feedback": feedback,
            "metrics": metrics_payload,
            "score": score_payload,
            "weights": evaluation["weights"],
            "scoring_mode": "single_question",
            "formula_check": evaluation["formula_check"],
            "model_used": model,
            "provider_base_url": DEFAULT_LLM_BASE_URL,
        }
        if is_near_zero_score(metrics_payload):
            response_payload = force_zero_score_response(
                response_payload,
                user_text,
                topper_text,
                max_marks,
                "single_question_near_zero_overlap",
            )
        if should_cache_compare_response(response_payload):
            _COMPARE_CACHE[cache_key] = response_payload
        response_payload = save_evaluation_result(user_text, topper_text, max_marks, response_payload, exam_name, subject)
        return jsonify(response_payload)
    except Exception as exc:
        return jsonify({"error": f"Comparison failed: {exc}"}), 500


@app.route("/ask-single-question", methods=["POST"])
def ask_single_question():
    data = request.get_json(silent=True) or {}
    user_text = (data.get("user_text") or "").strip()
    question_text = (data.get("question_text") or "").strip()

    if not question_text:
        return jsonify({"error": "question_text is required"}), 400

    try:
        client, model = get_llm_client()
        response = create_chat_completion(
            client=client,
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful LLM tutor, study assistant, and exam writing coach."},
                {"role": "user", "content": build_question_help_prompt(user_text, "", question_text)},
            ],
            temperature=0.3,
            prefer_json=False,
        )

        answer = clean_markdown_for_display(extract_message_content(response.choices[0].message) if response.choices else "")
        if not answer:
            raise RuntimeError("OpenAI returned an empty response.")

        ask_message = AskMessage(
            question_text=question_text,
            answer_text=answer,
            user_context=user_text,
            model_used=model,
        )
        db.session.add(ask_message)
        db.session.commit()

        return jsonify({"answer": answer, "ask_message_id": ask_message.id})
    except Exception as exc:
        return jsonify({"error": f"Question help failed: {exc}"}), 500


@app.route("/chat-assistant", methods=["POST"])
def chat_assistant():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    mode = normalize_spaces(data.get("mode") or "auto")
    session_id = data.get("session_id")
    user_text = (data.get("user_text") or "").strip()
    reference_text = (data.get("reference_text") or "").strip()
    comparison_context = (data.get("comparison_context") or "").strip()
    exam_name = normalize_spaces(data.get("exam_name"))
    subject = normalize_spaces(data.get("subject"))
    history = sanitize_chat_history(data.get("messages") or [])

    if not message:
        return jsonify({"error": "message is required"}), 400

    try:
        client, model = get_llm_client()
        context_message = build_chat_context_message(
            exam_name=exam_name,
            subject=subject,
            user_text=user_text,
            reference_text=reference_text,
            comparison_context=comparison_context,
        )

        messages = [{"role": "system", "content": build_chat_system_prompt(mode)}]
        if context_message:
            messages.append(
                {
                    "role": "system",
                    "content": f"Context from the current EvalAI workspace. Use this only if it helps answer the user's question.\n\n{context_message}",
                }
            )
        messages.extend(history)
        messages.append({"role": "user", "content": message})

        response = create_chat_completion(
            client=client,
            model=model,
            messages=messages,
            temperature=0.45,
            prefer_json=False,
        )
        answer = clean_markdown_for_display(extract_message_content(response.choices[0].message) if response.choices else "")
        if not answer:
            raise RuntimeError("The model returned an empty response.")

        saved_session_id = None
        if mode == "auto":
            ensure_chat_session_table()
            transcript = history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer},
            ]
            transcript = sanitize_chat_history(transcript, max_messages=40)

            chat_session = None
            if session_id not in (None, ""):
                try:
                    chat_session = ChatSession.query.get(int(session_id))
                except (TypeError, ValueError):
                    chat_session = None

            if chat_session is None:
                chat_session = ChatSession(
                    title=build_chat_session_title(transcript),
                    model_used=model,
                    messages=transcript,
                )
                db.session.add(chat_session)
            else:
                chat_session.title = build_chat_session_title(transcript)
                chat_session.model_used = model
                chat_session.messages = transcript

            db.session.commit()
            saved_session_id = chat_session.id

        return jsonify({"answer": answer, "model_used": model, "session_id": saved_session_id})
    except Exception as exc:
        return jsonify({"error": f"Chat failed: {exc}"}), 500


def serialize_timestamp(value):
    return value.isoformat() if value else None


def preview_text(value, limit=180):
    text = normalize_spaces(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def ensure_chat_session_table():
    ChatSession.__table__.create(bind=db.engine, checkfirst=True)


def build_chat_session_title(messages):
    for item in messages:
        if item.get("role") == "user":
            text = normalize_spaces(item.get("content") or "")
            if text:
                return preview_text(text, limit=70)
    return "Untitled chat"


@app.route("/history", methods=["GET"])
def history():
    limit = request.args.get("limit", 20, type=int)
    limit = max(1, min(limit or 20, 50))
    ensure_chat_session_table()

    evaluations = (
        Evaluation.query
        .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
        .limit(limit)
        .all()
    )
    chat_sessions = (
        ChatSession.query
        .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .limit(limit)
        .all()
    )
    ask_messages = (
        AskMessage.query
        .order_by(AskMessage.created_at.desc(), AskMessage.id.desc())
        .limit(limit)
        .all()
    )

    return jsonify(
        {
            "evaluations": [
                {
                    "id": item.id,
                    "exam_name": item.exam_name or "",
                    "subject": item.subject or "",
                    "label": " | ".join(part for part in (item.exam_name, item.subject) if part) or "Anonymous",
                    "earned_marks": item.earned_marks,
                    "max_marks": item.max_marks,
                    "percentage": item.percentage,
                    "coverage": item.coverage,
                    "accuracy": item.accuracy,
                    "feedback_preview": preview_text(item.feedback),
                    "created_at": serialize_timestamp(item.created_at),
                }
                for item in evaluations
            ],
            "chat_sessions": [
                {
                    "id": item.id,
                    "title": item.title or "Untitled chat",
                    "preview": preview_text(
                        next(
                            (
                                message.get("content") or ""
                                for message in reversed(item.messages or [])
                                if message.get("role") == "assistant"
                            ),
                            "",
                        )
                    ),
                    "message_count": len(item.messages or []),
                    "model_used": item.model_used or "",
                    "created_at": serialize_timestamp(item.created_at),
                    "updated_at": serialize_timestamp(item.updated_at),
                }
                for item in chat_sessions
            ],
            "ask_messages": [
                {
                    "id": item.id,
                    "question_text": item.question_text,
                    "answer_preview": preview_text(item.answer_text),
                    "has_uploaded_context": bool(item.user_context),
                    "model_used": item.model_used or "",
                    "created_at": serialize_timestamp(item.created_at),
                }
                for item in ask_messages
            ],
        }
    )


@app.route("/history/evaluations/<int:evaluation_id>", methods=["GET"])
def history_evaluation_detail(evaluation_id):
    item = Evaluation.query.get_or_404(evaluation_id)
    return jsonify(
        {
            "id": item.id,
            "exam_name": item.exam_name or "",
            "subject": item.subject or "",
            "user_text": item.user_text,
            "topper_text": item.topper_text,
            "max_marks": item.max_marks,
            "earned_marks": item.earned_marks,
            "percentage": item.percentage,
            "coverage": item.coverage,
            "accuracy": item.accuracy,
            "feedback": item.feedback or "",
            "result_payload": item.result_payload or {},
            "created_at": serialize_timestamp(item.created_at),
        }
    )


@app.route("/history/chat-sessions/<int:chat_session_id>", methods=["GET"])
def history_chat_session_detail(chat_session_id):
    ensure_chat_session_table()
    item = ChatSession.query.get_or_404(chat_session_id)
    return jsonify(
        {
            "id": item.id,
            "title": item.title or "Untitled chat",
            "model_used": item.model_used or "",
            "messages": item.messages or [],
            "created_at": serialize_timestamp(item.created_at),
            "updated_at": serialize_timestamp(item.updated_at),
        }
    )


@app.route("/history/ask-messages/<int:ask_message_id>", methods=["GET"])
def history_ask_message_detail(ask_message_id):
    item = AskMessage.query.get_or_404(ask_message_id)
    return jsonify(
        {
            "id": item.id,
            "question_text": item.question_text,
            "answer_text": item.answer_text or "",
            "user_context": item.user_context or "",
            "model_used": item.model_used or "",
            "created_at": serialize_timestamp(item.created_at),
        }
    )




@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_react(path):
    build_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'frontend', 'build'))
    requested = os.path.join(build_dir, path)
    if path and os.path.isfile(requested):
        return send_from_directory(build_dir, path)
    return send_from_directory(build_dir, 'index.html')

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
