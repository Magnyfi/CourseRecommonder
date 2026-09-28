'''
import time
from typing import List, Literal, Optional
 
from ollama import Client
from pydantic import BaseModel, Field, ValidationError
 
MODEL = "qwen2.5:7b"
NUM_CTX = 16384        # IMPORTANT: Ollama's default context is small and silently cuts long inputs
MAX_OUTPUT_TOKENS = 4000
 
 
# ---------- schema (same fields as your Gemini TOOL); Ollama constrains output to this ----------
class EvaluationComponent(BaseModel):
    name: str
    weightage_percent: Optional[float] = None
    duration: Optional[str] = None
    date: Optional[str] = None
    nature: Optional[str] = Field(None, description="open book / closed book / etc.")
 
 
class Course(BaseModel):
    course_code: Optional[str] = None
    course_title: Optional[str] = None
    attendance_policy: Optional[str] = None
    makeup_policy: Optional[str] = None
    syllabus: Optional[str] = Field(None, description="topics covered, concise")
    evaluation_components: List[EvaluationComponent] = Field(default_factory=list)
    page_reference: Optional[str] = Field(None, description="page numbers / section where the info was found")
    confidence: Literal["high", "medium", "low"]
    unreliable_fields: List[str] = Field(default_factory=list,
                                         description="fields that are ambiguous, garbled or uncertain")
 
 
class CourseList(BaseModel):
    courses: List[Course]
 
 
SYSTEM = (
    "You extract structured academic data from course documents (handouts, bulletins). "
    "Use ONLY information present in the text. Never guess or infer missing values: use null. "
    "If a value is present but ambiguous, garbled, or possibly wrong, still return your best "
    "reading but add the field name to unreliable_fields. Ignore irrelevant material "
    "(headers, footers, boilerplate, general notices). Text is tagged [page N | paragraph/table]. "
    "If the text contains no course information, return an empty list. "
    "Respond with JSON only, matching the provided schema."
)
 
# small local models sometimes write "N/A" instead of null - treat those as missing
_BLANKS = {"", "null", "none", "n/a", "na", "not specified", "not mentioned", "not available"}
 
 
def _clean(value):
    if isinstance(value, str) and value.strip().lower() in _BLANKS:
        return None
    return value
 
 
def make_client(host="http://localhost:11434"):
    return Client(host=host)
 
 
def extract_courses(client, text):
    t = time.time()
    resp = client.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": text},
        ],
        format=CourseList.model_json_schema(),          # <- structured output (replaces the forced tool call)
        options={"temperature": 0, "num_ctx": NUM_CTX, "num_predict": MAX_OUTPUT_TOKENS},
        stream=False,
    )
 
    print(f"llm call: {time.time() - t:.1f}s "
          f"(prompt tokens: {resp.prompt_eval_count}, output tokens: {resp.eval_count})")
    if resp.prompt_eval_count and resp.prompt_eval_count >= NUM_CTX - MAX_OUTPUT_TOKENS:
        print("  ! input may have been truncated - raise NUM_CTX or shorten the text")
 
    try:
        parsed = CourseList.model_validate_json(resp.message.content)
    except ValidationError as e:                        # e.g. output cut off by num_predict
        print(f"  ! model output did not match the schema: {e.error_count()} error(s)")
        return []
 
    courses = []
    for c in parsed.courses:
        d = c.model_dump()
        d = {k: _clean(v) for k, v in d.items()}
        for comp in d["evaluation_components"]:
            for k in comp:
                comp[k] = _clean(comp[k])
        courses.append(d)
    return courses
 
'''
#if __name__ == "__main__":
#    sample = "[page 1 | paragraph]\nCS F212 Data Structures. Attendance is not compulsory.\n\n[page 2 | table]\n| Component | % |\n| --- | --- |\n| Midsem | 30 |"
#    print(extract_courses(make_client(), sample))

import time

from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

MODEL = "gemini-3.6-flash"

TOOL = {
    "function_declarations": [
        {
            "name": "record_courses",
            "description": "Record every course found in the supplied document text.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "courses": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "course_code": {"type": "STRING", "nullable": True},
                                "course_title": {"type": "STRING", "nullable": True},
                                "attendance_policy": {"type": "STRING", "nullable": True},
                                "makeup_policy": {"type": "STRING", "nullable": True},
                                "syllabus": {
                                    "type": "STRING",
                                    "nullable": True,
                                    "description": "topics covered, concise",
                                },
                                "evaluation_components": {
                                    "type": "ARRAY",
                                    "items": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "name": {"type": "STRING"},
                                            "weightage_percent": {
                                                "type": "NUMBER",
                                                "nullable": True,
                                            },
                                            "duration": {"type": "STRING", "nullable": True},
                                            "date": {"type": "STRING", "nullable": True},
                                            "nature": {
                                                "type": "STRING",
                                                "nullable": True,
                                                "description": "open book / closed book / etc.",
                                            },
                                        },
                                        "required": ["name"],
                                    },
                                },
                                "page_reference": {
                                    "type": "STRING",
                                    "nullable": True,
                                    "description": "page numbers / section where the info was found",
                                },
                                "confidence": {
                                    "type": "STRING",
                                    "enum": ["high", "medium", "low"],
                                },
                                "unreliable_fields": {
                                    "type": "ARRAY",
                                    "items": {"type": "STRING"},
                                    "description": "fields that are ambiguous, garbled or uncertain",
                                },
                            },
                            "required": ["course_code", "confidence", "unreliable_fields"],
                        }
                    }
                },
                "required": ["courses"],
            },
        }
    ]
}

SYSTEM = (
    "You extract structured academic data from course documents (handouts, bulletins). "
    "Use ONLY information present in the text. Never guess or infer missing values: use null. "
    "If a value is present but ambiguous, garbled, or possibly wrong, still return your best "
    "reading but add the field name to unreliable_fields. Ignore irrelevant material "
    "(headers, footers, boilerplate, general notices). Text is tagged [page N | paragraph/table]. "
    "If the text contains no course information, return an empty list."
)


def extract_courses(client, text):

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        max_output_tokens=8000,
        tools=[TOOL],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode="ANY", # Forces the model to use a tool
                allowed_function_names=["record_courses"]
            )
        ),
        # Disable Automatic Function Calling (AFC) to capture arguments
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
    )
    t = time.time()
    resp = client.models.generate_content(
        model=MODEL,
        contents=text,
        config=config,
    )

    print(f"llm call: {time.time() - t:.1f}s")
    print(resp.usage_metadata)

    # Correctly parse function calls in the google-genai SDK
    if resp.function_calls:
        for function_call in resp.function_calls:
            if function_call.name == "record_courses":
                # function_call.args is a dictionary containing the extracted parameters
                return function_call.args.get("courses", [])
    
    return []

