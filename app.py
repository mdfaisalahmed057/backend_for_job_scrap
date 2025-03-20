from flask import Flask, request, jsonify
import fitz  # PyMuPDF for PDF parsing
import docx
import os
import google.generativeai as genai
import json
import requests
from bs4 import BeautifulSoup
import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urljoin
import time
from dotenv import load_dotenv
import random
from flask_cors import CORS
import hashlib
import psutil

from concurrent.futures import ThreadPoolExecutor
import logging

load_dotenv()
app = Flask(__name__)
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY environment variable is not set")

genai.configure(api_key=api_key)
CORS(app)  # Enables CORS for all routes
 

# User agent rotation to avoid being blocked
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.45 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:94.0) Gecko/20100101 Firefox/94.0'
]

# Default job portals to scrape if no custom URL is provided
DEFAULT_JOB_PORTALS = {
    'indeed': 'https://www.indeed.com/jobs?q={role}+{skills}&l={location}',
    'linkedin': 'https://www.linkedin.com/jobs/search/?keywords={role}%20{skills}&location={location}',
    'glassdoor': 'https://www.glassdoor.com/Job/jobs.htm?sc.keyword={role}%20{skills}&locT=C&locId={location}'
}

# Rate limiting settings
MIN_REQUEST_INTERVAL = 2  # Increased to 2 seconds between requests
MAX_CONCURRENT_REQUESTS = 3  # Reduced to 3 to be more respectful

# Memory monitoring
def get_memory_usage():
    """Get current memory usage in MB"""
    process = psutil.Process()
    return process.memory_info().rss / (1024 * 1024)  # Convert to MB

class JobScraper:
    def __init__(self):
        self.session = requests.Session()
        self.last_request_time = {}  # Track last request time per domain
        self.failed_requests = []  # Track failed requests
        
    def get_random_user_agent(self):
        """Return a random user agent from the list."""
        return random.choice(USER_AGENTS)
    
    def respect_rate_limits(self, domain):
        """Ensure we don't hammer a domain with requests."""
        current_time = time.time()
        if domain in self.last_request_time:
            elapsed = current_time - self.last_request_time[domain]
            if elapsed < MIN_REQUEST_INTERVAL:
                time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self.last_request_time[domain] = time.time()
    
    def fetch_page(self, url):
        """Fetch a page with proper headers and rate limiting."""
        domain = urlparse(url).netloc
        self.respect_rate_limits(domain)
        headers = {
            'User-Agent': self.get_random_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.google.com/',
            'Connection': 'keep-alive',
            'Cache-Control': 'max-age=0'
        }
        try:
            response = self.session.get(url, headers=headers, timeout=15)
            status_code = response.status_code
            
            if status_code == 403:
                # Return information about the forbidden URL
                self.failed_requests.append({
                    'url': url,
                    'status': 403,
                    'message': 'Forbidden - Access denied',
                    'portal': domain
                })
                return {'forbidden': True, 'url': url, 'domain': domain}
            
            if status_code != 200:
                self.failed_requests.append({
                    'url': url,
                    'status': status_code,
                    'message': f'HTTP error {status_code}',
                    'portal': domain
                })
                return None
                
            return response.text
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            self.failed_requests.append({
                'url': url,
                'status': 'error',
                'message': error_message,
                'portal': domain
            })
            print(f"Error fetching {url}: {e}")
            return None
    
    def parse_indeed_job(self, job_element):
        """Parse job data from Indeed."""
        try:
            job_data = {}
            # Updated Indeed selectors based on more recent Indeed structure
            title_elem = job_element.select_one('.jcs-JobTitle') or job_element.select_one('h2.jobTitle')
            job_data['job_title'] = title_elem.get_text().strip() if title_elem else "N/A"
            
            company_elem = job_element.select_one('.companyName') or job_element.select_one('span[data-testid="company-name"]')
            job_data['company'] = company_elem.get_text().strip() if company_elem else "N/A"
            
            location_elem = job_element.select_one('.companyLocation') or job_element.select_one('div[data-testid="text-location"]')
            job_data['location'] = location_elem.get_text().strip() if location_elem else "N/A"
            
            job_link = job_element.select_one('a.jcs-JobTitle') or job_element.select_one('a[data-jk]')
            if job_link and job_link.get('href'):
                job_data['application_link'] = urljoin('https://www.indeed.com', job_link.get('href'))
            else:
                job_data['application_link'] = "N/A"
            
            date_elem = job_element.select_one('span.date') or job_element.select_one('span[data-testid="job-age"]')
            job_data['posting_date'] = date_elem.get_text().strip() if date_elem else "N/A"
            
            job_data['job_type'] = self.extract_job_type(job_element)
            job_data['required_skills'] = []  # Placeholder – would require extra parsing
            job_data['job_description'] = "Visit job page for full description"
            job_data['salary_range'] = self.extract_salary(job_element)
            job_data['status'] = "Pending"
            
            return job_data
        except Exception as e:
            print(f"Error parsing Indeed job: {e}")
            return None
    
    def parse_linkedin_job(self, job_element):
        """Parse job data from LinkedIn."""
        try:
            job_data = {}
            # Updated LinkedIn selectors based on more recent LinkedIn structure
            title_elem = job_element.select_one('.base-search-card__title') or job_element.select_one('.job-card-list__title')
            job_data['job_title'] = title_elem.get_text().strip() if title_elem else "N/A"
            
            company_elem = job_element.select_one('.base-search-card__subtitle') or job_element.select_one('.job-card-container__company-name')
            job_data['company'] = company_elem.get_text().strip() if company_elem else "N/A"
            
            location_elem = job_element.select_one('.job-search-card__location') or job_element.select_one('.job-card-container__metadata-item')
            job_data['location'] = location_elem.get_text().strip() if location_elem else "N/A"
            
            job_link = job_element.select_one('a.base-card__full-link') or job_element.select_one('.job-card-container__link')
            job_data['application_link'] = job_link.get('href') if job_link else "N/A"
            
            date_elem = job_element.select_one('.job-search-card__listdate') or job_element.select_one('time')
            job_data['posting_date'] = date_elem.get('datetime') if date_elem and date_elem.get('datetime') else "N/A"
            
            job_data['job_type'] = "N/A"  # Placeholder – additional details may be needed
            job_data['required_skills'] = []  # Placeholder
            job_data['job_description'] = "Visit job page for full description"
            job_data['salary_range'] = "N/A"  # Placeholder
            job_data['status'] = "Pending"
            
            return job_data
        except Exception as e:
            print(f"Error parsing LinkedIn job: {e}")
            return None
    
    def parse_glassdoor_job(self, job_element):
        """Parse job data from Glassdoor."""
        try:
            job_data = {}
            # Updated Glassdoor selectors based on more recent Glassdoor structure
            title_elem = job_element.select_one('.jobLink') or job_element.select_one('.job-title')
            job_data['job_title'] = title_elem.get_text().strip() if title_elem else "N/A"
            
            company_elem = job_element.select_one('.employerName') or job_element.select_one('.employer-name')
            job_data['company'] = company_elem.get_text().strip() if company_elem else "N/A"
            
            location_elem = job_element.select_one('.location') or job_element.select_one('.job-location')
            job_data['location'] = location_elem.get_text().strip() if location_elem else "N/A"
            
            job_link = job_element.select_one('a.jobLink') or job_element.select_one('a.job-link')
            job_data['application_link'] = urljoin('https://www.glassdoor.com', job_link.get('href')) if job_link else "N/A"
            
            job_data['job_type'] = "N/A"
            job_data['required_skills'] = []  # Placeholder
            job_data['job_description'] = "Visit job page for full description"
            job_data['salary_range'] = "N/A"
            job_data['posting_date'] = "N/A"
            job_data['status'] = "Pending"
            
            return job_data
        except Exception as e:
            print(f"Error parsing Glassdoor job: {e}")
            return None
    
    def parse_generic_job(self, job_element, selectors):
        """Parse job data using custom selectors for generic job sites."""
        try:
            job_data = {}
            for field, selector in selectors.items():
                elem = job_element.select_one(selector)
                job_data[field] = elem.get_text().strip() if elem else "N/A"
            if 'required_skills' not in job_data:
                job_data['required_skills'] = []
            if 'status' not in job_data:
                job_data['status'] = "Pending"
            return job_data
        except Exception as e:
            print(f"Error parsing generic job: {e}")
            return None
    
    def extract_job_type(self, job_element):
        """Extract job type from job element text."""
        job_type_patterns = ['full-time', 'part-time', 'contract', 'temporary', 'internship', 'remote']
        job_text = job_element.get_text().lower()
        for pattern in job_type_patterns:
            if pattern in job_text:
                return pattern.title()
        return "N/A"
    
    def extract_salary(self, job_element):
        """Extract salary information from job element."""
        salary_elem = job_element.select_one('.salary-snippet') or job_element.select_one('[data-testid="salary-estimate"]')
        if salary_elem:
            return salary_elem.get_text().strip()
        job_text = job_element.get_text()
        salary_pattern = r'\$\d+(?:,\d+)?(?:\s*-\s*\$\d+(?:,\d+)?)?(?:\s*(?:per|a|\/)\s*(?:year|month|hour|yr|hr|week|wk))?'
        salary_match = re.search(salary_pattern, job_text)
        if salary_match:
            return salary_match.group(0)
        return "N/A"
    
    



    def scrape_jobs(self, role, skills, location, num_jobs=10, custom_urls=None):
        """Scrape jobs from multiple sources based on role, skills, and location."""
        all_jobs = []
        urls_to_scrape = []
        
        # Prepare skills list and patterns for filtering
        skills_list = skills if isinstance(skills, list) else [s.strip() for s in skills.split(',')]
        skills_str = '+'.join(skills_list)
        skills_pattern = re.compile('|'.join([re.escape(skill) for skill in skills_list if skill]), re.IGNORECASE)
        location_pattern = re.compile(re.escape(location), re.IGNORECASE)
        
        # Prepare default URLs for known portals
        for portal, url_template in DEFAULT_JOB_PORTALS.items():
            url = url_template.format(
                role=role.replace(' ', '+'),
                skills=skills_str,
                location=location.replace(' ', '+')
            )
            urls_to_scrape.append((portal, url))
        
        # Add custom URLs if provided
        if custom_urls:
            if isinstance(custom_urls, str):
                custom_urls = [custom_urls]
            for url in custom_urls:
                urls_to_scrape.append(('custom', url))
        
        # Use a thread pool to scrape concurrently
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS) as executor:
            futures = []
            for portal, url in urls_to_scrape:
                futures.append(executor.submit(self.scrape_single_source, portal, url, num_jobs, skills_pattern, location_pattern))
            
            for future in futures:
                result = future.result()
                if result:
                    all_jobs.extend(result)
                
                if len(all_jobs) >= num_jobs:
                    all_jobs = all_jobs[:num_jobs]
                    break
                
                current_memory = get_memory_usage()
                print(f"Current memory usage: {current_memory:.2f} MB")
                if current_memory > 14000:  # Limit memory usage to 14GB
                    print("Memory usage too high, stopping job collection")
                    break
        
        # Include information about access-denied URLs
        return {
            'jobs': all_jobs,
            'failed_requests': self.failed_requests
        }
    
    def scrape_single_source(self, portal, url, max_jobs, skills_pattern, location_pattern):
        """Scrape a single job portal."""
        print(f"Scraping {portal}: {url}")
        html_response = self.fetch_page(url)
        
        if not html_response:
            return []
            
        # Handle forbidden URLs
        if isinstance(html_response, dict) and html_response.get('forbidden'):
            return []
            
        # Process HTML response
        soup = BeautifulSoup(html_response, 'html.parser')
        jobs_found = []
        
        try:
            # Updated selectors for job elements
            job_elements_selectors = {
                'indeed': ['div.job_seen_beacon', 'div.jobsearch-SerpJobCard', 'div[data-testid="job-card"]'],
                'linkedin': ['div.base-search-card', 'li.jobs-search-results__list-item', 'div.job-card-container'],
                'glassdoor': ['li.react-job-listing', 'div.jobCard', 'li.jl', 'article.jobCard']
            }
            
            # Try multiple selectors for each portal
            job_elements = []
            if portal in job_elements_selectors:
                for selector in job_elements_selectors[portal]:
                    job_elements = soup.select(selector)
                    if job_elements:
                        print(f"Found {len(job_elements)} job elements with selector '{selector}' for {portal}")
                        break
            
            # If no elements found with specific selectors, try generic selectors
            if not job_elements:
                job_elements = soup.select('div.job') or soup.select('div[class*="job"]') or soup.select('li[class*="job"]')
                print(f"Using generic selectors, found {len(job_elements)} job elements for {portal}")
            
            # Parse jobs based on portal
            if portal == 'indeed':
                parse_func = self.parse_indeed_job
            elif portal == 'linkedin':
                parse_func = self.parse_linkedin_job
            elif portal == 'glassdoor':
                parse_func = self.parse_glassdoor_job
            else:
                parse_func = lambda elem: self.parse_generic_job(elem, {
                    'job_title': 'h2, h3, .title, [class*="title"]',
                    'company': 'span.company, div.company, [class*="company"]',
                    'location': 'div.location, span.location, [class*="location"]',
                    'posting_date': 'span.date, time, [class*="date"]'
                })
            
            # Parse jobs
            for elem in job_elements:
                job = parse_func(elem)
                if job:
                    jobs_found.append(job)
                    if len(jobs_found) >= max_jobs:
                        break
            
            # Store the raw HTML for debugging if no jobs found
            if not jobs_found and job_elements:
                print(f"Found elements but couldn't parse jobs for {portal}. First element: {job_elements[0]}")
        
        except Exception as e:
            self.failed_requests.append({
                'url': url,
                'status': 'parsing_error',
                'message': str(e),
                'portal': portal
            })
            print(f"Error scraping {portal}: {e}")
        
        print(f"Found {len(jobs_found)} relevant jobs from {portal}")
        return jobs_found[:max_jobs]

def preprocess_with_llm(role, location, skills):
    """
    Use Gemini to preprocess and standardize job search inputs.
    """
    # For now, skip LLM processing and just return standard format
    skills_list = skills if isinstance(skills, list) else [s.strip() for s in skills.split(',') if s.strip()]
    
    return {
        "role": role,
        "location": location.split(',')[0].strip() if isinstance(location, str) else location,
        "skills": skills_list
    }
    
@app.route('/api/jobs', methods=['POST'])
def get_jobs():
    """API endpoint to get jobs based on criteria from POST payload."""
    try:
        payload = request.get_json()
        if not payload:
            return jsonify({
                'error': 'Missing JSON payload',
                'required_fields': ['role', 'location'],
                'optional_fields': ['skills', 'num_jobs', 'urls']
            }), 400
            
        # Get raw inputs
        raw_role = payload.get('role')
        raw_location = payload.get('location')
        raw_skills = payload.get('skills', [])
        num_jobs = payload.get('num_jobs', 10)
        custom_urls = payload.get('urls')
        
        if not raw_role or not raw_location:
            return jsonify({
                'error': 'Missing required fields in payload',
                'required': ['role', 'location'],
                'optional': ['skills', 'num_jobs', 'urls']
            }), 400
        
        # Preprocess inputs
        processed_data = preprocess_with_llm(raw_role, raw_location, raw_skills)
        
        role = processed_data['role']
        location = processed_data['location']
        skills = processed_data['skills']
        
        # Log the transformation for debugging
        print(f"Original input: {raw_role}, {raw_location}, {raw_skills}")
        print(f"Processed input: {role}, {location}, {skills}")
        
        scraper = JobScraper()
        result = scraper.scrape_jobs(role, skills, location, num_jobs, custom_urls)
        
        jobs = result.get('jobs', [])
        failed_requests = result.get('failed_requests', [])
        
        return jsonify({
            'status': 'success',
            'count': len(jobs),
            'jobs': jobs,
            'failed_requests': failed_requests,
            'processed_query': {
                'role': role,
                'location': location,
                'skills': skills
            }
        })
    except Exception as e:
        import traceback
        stack_trace = traceback.format_exc()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'stack_trace': stack_trace
        }), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    memory_usage = get_memory_usage()
    return jsonify({
        'status': 'online',
        'memory_usage_mb': memory_usage,
        'timestamp': time.time()
    })

 
 

def extract_text_from_pdf(pdf_path):
    text = ""
    doc = fitz.open(pdf_path)
    for page in doc:
        text += page.get_text("text")
    return text

def extract_text_from_docx(docx_path):
    doc = docx.Document(docx_path)
    return "\n".join([para.text for para in doc.paragraphs])

def extract_resume_text(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        return extract_text_from_pdf(file_path)
    elif ext == ".docx":
        return extract_text_from_docx(file_path)
    else:
        return None

import json

def get_structured_resume_data(text):
    prompt = f"""
    Extract and format the following resume details from the text in **strict JSON format**:
    Name, Email, Phone, Location, LinkedIn, Portfolio, Skills (Technical & Soft),
    Education, Work Experience, Certifications, Projects, Languages Known.
    
    **Important:**
    - Output must be valid JSON.
    - Do **not** include any explanations or extra text.
    - Do **not** wrap JSON inside a code block.
    - Ensure proper nesting and data formatting.

    **Resume Text:**
    {text}

    **Expected Output Format:**
    {{
      "name": "John Doe",
      "email": "john.doe@example.com",
      "phone": "+1234567890",
      "location": "New York, USA",
      "linkedin": "john-doe",
      "portfolio": "https://johndoe.dev/",
      "skills": {{
        "technical": ["JavaScript", "React.js", "Node.js"],
        "soft": ["Communication", "Problem-solving"]
      }},
      "education": [
        {{
          "institution": "XYZ University",
          "location": "New York, USA",
          "degree": "B.Sc in Computer Science",
          "duration": "2018 - 2022",
          "grade": "3.8/4.0"
        }}
      ],
      "work_experience": [
        {{
          "title": "Software Engineer",
          "company": "TechCorp",
          "location": "San Francisco, USA",
          "duration": "Jan 2022 – Present",
          "description": "Developed full-stack applications with React and Node.js."
        }}
      ],
      "certifications": ["AWS Certified Developer"],
      "projects": [
        {{
          "name": "E-Commerce Platform",
          "technologies": ["React", "Node.js", "MongoDB"],
          "description": "Built a fully functional e-commerce website with authentication and payment integration."
        }}
      ],
      "languages_known": ["English", "Spanish"]
    }}
    """

    model = genai.GenerativeModel("gemini-2.0-flash")
    response = model.generate_content(prompt)
    
    # Clean up the response and extract just the JSON part
    raw_text = response.text.strip()
    
    # Try to find JSON in the response by looking for balanced braces
    start_idx = raw_text.find('{')
    if start_idx == -1:
        return {"error": "Could not find JSON structure in the response"}
    
    # Track opening and closing braces to find the complete JSON object
    open_braces = 0
    for i in range(start_idx, len(raw_text)):
        if raw_text[i] == '{':
            open_braces += 1
        elif raw_text[i] == '}':
            open_braces -= 1
            if open_braces == 0:
                # We found the matching closing brace for the first opening brace
                json_str = raw_text[start_idx:i+1]
                break
    else:
        # If we didn't break out of the loop, we didn't find balanced braces
        return {"error": "Unbalanced JSON structure in response"}
    
    # Try parsing the extracted JSON
    try:
        structured_data = json.loads(json_str)
        return structured_data
    except json.JSONDecodeError as e:
        # If parsing fails, return a detailed error message
        return {
            "error": f"JSON parsing failed: {str(e)}",
            "raw_response": raw_text[:100] + "..." if len(raw_text) > 100 else raw_text
        }

@app.route("/extract_resume", methods=["POST"])
def extract_resume():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    
    # Ensure uploads directory exists
    upload_dir = "uploads"
    if not os.path.exists(upload_dir):
        os.makedirs(upload_dir)

    file_path = os.path.join(upload_dir, file.filename)
    file.save(file_path)
    
    text = extract_resume_text(file_path)
    if text is None:
        return jsonify({"error": "Unsupported file format"}), 400
    
    structured_data = get_structured_resume_data(text)
    
    # Remove file after processing
    os.remove(file_path)
    
    return jsonify(structured_data)

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files["file"]
    file_path = f"uploads/{file.filename}"
    file.save(file_path)
    
    text = extract_resume_text(file_path)
    if text is None:
        return jsonify({"error": "Unsupported file format"}), 400
    
    structured_data = get_structured_resume_data(text)
    os.remove(file_path)  # Clean up after processing
    return jsonify(structured_data)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))  # Get port from environment, default to 10000
    app.run(host="0.0.0.0", port=port) 
