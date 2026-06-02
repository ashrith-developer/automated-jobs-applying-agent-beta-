from typing import Literal, Optional
from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI
import json
import sys
import os
from dotenv import load_dotenv

# Ensure environment variables are loaded in standalone execution
load_dotenv()

class GuardrailedFormResponse(BaseModel):
    """
    Structured validation schema for job screening form answers.
    Ensures that generated answers strictly match the requested database type.
    """
    raw_answer: str = Field(description="The exact direct value to enter into the form field. Avoid prefixes, quotes, or conversational text.")
    data_type: Literal["boolean", "number", "text", "select"] = Field(description="The target category of the input field.")
    explanation: Optional[str] = Field(description="Brief internal explanation or context for this answer.")

def get_gemini_chat_model(api_key: str = None):
    """
    Initializes a LangChain Google GenAI Chat model with the given API key or environment variable.
    """
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError("Gemini API key is required but missing. Configure it in .env or settings.")
    return ChatGoogleGenerativeAI(
        model="gemini-1.5-flash",
        google_api_key=key,
        temperature=0.0,  # Strict temperature for deterministic form filling
        max_retries=3
    )

def research_company_context(company_name: str, tavily_api_key: str = None) -> str:
    """
    Uses Tavily to fetch clean, relevant web details about a company.
    """
    key = tavily_api_key or os.environ.get("TAVILY_API_KEY")
    if not key:
        return ""
    try:
        from langchain_community.tools.tavily_search import TavilySearchResults
        # Set the environment variable just in case LangChain expects it
        os.environ["TAVILY_API_KEY"] = key
        search = TavilySearchResults(max_results=2)
        query = f"{company_name} company mission products engineering stack recent news"
        results = search.invoke({"query": query})
        
        context_blocks = []
        for r in results:
            context_blocks.append(f"Source: {r['url']}\nContent: {r['content']}")
        return "\n\n".join(context_blocks)
    except Exception as e:
        print(f"⚠️ Tavily search failed for company '{company_name}': {e}")
        return ""

def answer_question(question: str, field_type: str, profile_data: dict, api_key: str = None, company_name: str = None) -> str:
    """
    Given a job application screening question, uses LangChain ChatGoogleGenerativeAI
    and Pydantic Guardrails to answer it strictly and structurally based on the profile.
    """
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        return "No API key configured"
        
    try:
        chat = get_gemini_chat_model(key)
        
        # Structure the target field category for LLM guidance
        field_type_lower = field_type.lower()
        if "bool" in field_type_lower or "radio" in field_type_lower or "check" in field_type_lower:
            std_type = "boolean"
            type_rules = "MUST output exactly 'Yes' or 'No'. Never leave blank."
        elif "num" in field_type_lower or "year" in field_type_lower or "count" in field_type_lower:
            std_type = "number"
            type_rules = "MUST output a single plain integer string (e.g. '3' or '0'). Do not write text, words, or ranges like '2-3'."
        elif "select" in field_type_lower or "drop" in field_type_lower:
            std_type = "select"
            type_rules = "Output the direct selection item that best matches the candidate context."
        else:
            std_type = "text"
            type_rules = "Output a short, concise description under 1-2 sentences maximum."
            
        profile_summary = json.dumps({
            "personal": profile_data.get("personal", {}),
            "education": profile_data.get("education", []),
            "experience": profile_data.get("experience", []),
            "skills": profile_data.get("skills", []),
            "standard_qa": profile_data.get("standard_qa", {})
        }, indent=2)
        
        # Real-time Company Research via Tavily Search
        company_context = ""
        tavily_key = os.environ.get("TAVILY_API_KEY")
        if company_name and company_name != "Unknown Company" and tavily_key:
            print(f"🔍 [Tavily Search] Researching '{company_name}' for context...")
            company_context = research_company_context(company_name, tavily_key)

        company_info_prompt = ""
        if company_context:
            company_info_prompt = f"""
Here is some live researched context about the company ({company_name}) to help answer the question accurately:
{company_context}
"""
        
        prompt = f"""You are an automated job application assistant. Answer this screening question based strictly and truthfully on the candidate's profile and current status.

Candidate Profile Summary:
```json
{profile_summary}
```
{company_info_prompt}

CRITICAL CANDIDATE STATUS INFORMATION:
- The candidate is CURRENTLY A STUDENT and is CURRENTLY UNEMPLOYED.
- Any question related to their CURRENT ROLE, PRESENT JOB, CURRENT SALARY, CURRENT ANNUAL CTC, CURRENT EMPLOYER, or PRESENT EMPLOYMENT STATUS must be answered based on the fact that they are a student and unemployed.
  - For example, if asked for current salary or current CTC, return "0".
  - If asked for current job title or current employer, return "Student" or "None".
  - If asked for notice period or start date, they can start immediately (notice period is "0" days or "Immediate").
- For expected, desired, or target salary, refer to the desired salary in the profile ("900,000 INR" or "9 Lakhs").

The question you need to solve is:
"{question}"


Enforced Standard Data Type: "{std_type}"
Validation Rules: {type_rules}

Instructions:
1. Extract or reasonably infer the correct value from the candidate profile context.
2. If the question asks specifically about the company (e.g., 'Why do you want to work here?', or 'What is our mission?'), utilize the researched company context provided above to formulate a highly compelling and truthful response matching the candidate's profile.
3. If the profile does not mention any relevant details, infer a logical default (e.g., '0' for number of experience, 'No' for visa sponsorship, or 'Yes' if reasonable).
4. Populate the structured schema with the final formatted 'raw_answer' according to the validation rules.
"""
        # Bind the Pydantic structured output model as a guardrail
        structured_llm = chat.with_structured_output(GuardrailedFormResponse)
        response = structured_llm.invoke(prompt)
        
        # Verify the guardrailed response matches standard constraint rules
        ans = response.raw_answer.strip()
        if std_type == "boolean":
            # Sanitize to binary Yes/No
            if "yes" in ans.lower() or "true" in ans.lower() or ans.lower() == "y":
                return "Yes"
            return "No"
            
        return ans
    except Exception as e:
        print(f"LangChain Structured LLM Error answering question '{question}': {e}")
        # Direct heuristic fallbacks in case of API failure
        if "bool" in field_type.lower():
            return "Yes"
        elif "num" in field_type.lower():
            return "2"
        return "Yes"

def generate_cover_letter(job_title: str, company: str, job_description: str, profile_data: dict, api_key: str = None) -> str:
    """
    Generates a personalized, professional cover letter tailored to a specific job using LangChain.
    """
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        return "Configure your Gemini API key in settings to auto-generate personalized cover letters!"
        
    try:
        chat = get_gemini_chat_model(key)
        
        profile_summary = json.dumps({
            "personal": profile_data.get("personal", {}),
            "education": profile_data.get("education", []),
            "experience": profile_data.get("experience", []),
            "skills": profile_data.get("skills", [])
        }, indent=2)
        
        prompt = f"""You are an AI career assistant. Write a short, highly-compelling, and customized cover letter for a candidate applying to a job.

Job Details:
- Title: {job_title}
- Company: {company}
- Job Description snippet: {job_description[:1000]}

Candidate Profile Details:
```json
{profile_summary}
```

Rules:
1. Write a professional, modern, and concise cover letter (approx. 150-220 words).
2. Directly align 1 or 2 key skills or experiences from the candidate's profile with the job's key requirements.
3. Use a confident, professional, and humble tone. Avoid generic templates.
4. Format with a professional greeting and closing (e.g., "Dear Hiring Team at {company}," and "Best regards, [Candidate Name]").
5. Do not include placeholders like "[Date]" or "[Address]". Fill in actual values if known, or omit them.
6. Return only the plain text of the cover letter, no markdown formatting tags.
7. Integrate any specific aspects of the company context if you can.
"""
        response = chat.invoke(prompt)
        return response.content.strip()
    except Exception as e:
        print(f"LangChain Cover Letter Generation Error: {e}")
        return f"Dear Hiring Team,\n\nI am extremely excited to apply for the {job_title} role at {company}. My skills in software development make me a strong candidate for this position.\n\nBest regards,\n{profile_data.get('personal', {}).get('first_name', '')} {profile_data.get('personal', {}).get('last_name', '')}"
