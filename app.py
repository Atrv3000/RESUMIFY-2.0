import bleach
from markupsafe import escape 
import json
from datetime import datetime
import os
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from config import FREE_TEMPLATES
from forms import LoginForm, RegisterForm
from models import db, User, Resume, Purchase


def sanitize_input(text, max_length=5000):
    """Sanitize user input to prevent XSS attacks"""
    if not text or not isinstance(text, str):
        return text
    
    # Truncate to max length
    text = text[:max_length]
    
    # Allow only basic formatting tags
    allowed_tags = ['b', 'i', 'u', 'br', 'p', 'strong', 'em']
    allowed_attrs = {}
    
    # Clean HTML but preserve basic formatting
    cleaned = bleach.clean(
        text, 
        tags=allowed_tags,
        attributes=allowed_attrs,
        strip=True
    )
    
    return cleaned
# ==============================
# 🔧 App Setup
# ==============================
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or "dev_key"
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///Resumify.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
db.init_app(app)

# ==============================
# 🔐 Login Manager
# ==============================
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ==============================
#  AI Utility
# ==============================
def get_ai_response(prompt, system_prompt, temperature=0.7, max_tokens=200, max_retries=3):
    """
    Call OpenRouter API with retry logic and better error handling
    
    Args:
        prompt: User prompt
        system_prompt: System instructions for AI
        temperature: Creativity level (0.0 - 1.0)
        max_tokens: Maximum response length
        max_retries: Number of retry attempts
    
    Returns:
        AI generated text or None if failed
    """
    api_key = os.getenv('OPENROUTER_API_KEY')
    
    if not api_key:
        print("[ERROR] OPENROUTER_API_KEY not found in environment variables")
        return None
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:5000",
        "X-Title": "Resumify"
    }
    
    payload = {
        "model": "deepseek/deepseek-chat",  # Using paid model for better quality
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    
    for attempt in range(max_retries):
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=15  # Increased timeout
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result["choices"][0]["message"]["content"].strip()
                
                # Validate response
                if content and len(content) > 10:
                    return content
                else:
                    print(f"[WARNING] AI response too short: {content}")
                    continue
                    
            elif response.status_code == 429:
                # Rate limit - wait and retry
                print(f"[WARNING] Rate limited, attempt {attempt + 1}/{max_retries}")
                import time
                time.sleep(2 ** attempt)  # Exponential backoff
                continue
                
            else:
                print(f"[ERROR] API returned status {response.status_code}: {response.text}")
                
        except requests.exceptions.Timeout:
            print(f"[ERROR] Request timeout, attempt {attempt + 1}/{max_retries}")
            continue
            
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Request failed: {str(e)}")
            continue
            
        except Exception as e:
            print(f"[ERROR] Unexpected error: {str(e)}")
            continue
    
    print("[ERROR] All retry attempts failed")
    return None

# ==============================
# 🌐 Routes
# ==============================

@app.route('/')
def index():
    return render_template("index.html")


@app.route('/start')
@login_required
def start():
    return render_template("start.html")


# In app.py, update the /generate route to handle multiple experiences:

@app.route('/generate', methods=['POST'])
@login_required
def generate():
    try:
        # --- Step 1: Refresh tokens if needed ---
        current_user.reset_tokens_if_needed()
        db.session.commit()

        form = request.form
        template = form.get('template', 'classic')

        # --- Step 2: Premium template check ---
        is_premium = template not in FREE_TEMPLATES
        if is_premium and not (current_user.is_pro_user() or current_user.is_ultimate_user()):
            flash("This template is only available for Pro or Ultimate users.", "error")
            return redirect(url_for('pricing'))

        # --- Step 3: Token availability check ---
        if not current_user.has_tokens():
            flash("You're out of tokens. Please buy more or wait for your daily reset.", "error")
            return redirect(url_for('pricing'))

        # --- Step 4: Profile picture handling ---
        pic_url = None
        if 'profile_pic' in request.files:
            pic = request.files['profile_pic']
            if pic and pic.filename:
                # Validate file type
                allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
                file_ext = pic.filename.rsplit('.', 1)[1].lower() if '.' in pic.filename else ''
                
                if file_ext not in allowed_extensions:
                    flash("Invalid file type. Please upload an image (PNG, JPG, JPEG, GIF, WEBP).", "error")
                    return redirect(url_for('start'))
                
                # Limit file size (5MB)
                pic.seek(0, os.SEEK_END)
                file_size = pic.tell()
                pic.seek(0)
                
                if file_size > 5 * 1024 * 1024:  # 5MB
                    flash("File too large. Maximum size is 5MB.", "error")
                    return redirect(url_for('start'))
                
                filename = secure_filename(pic.filename)
                # Add timestamp to avoid filename conflicts
                timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
                filename = f"{timestamp}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                pic.save(filepath)
                pic_url = f"/static/uploads/{filename}"

        # --- Step 5: Bio generation (fallback to AI if empty) ---
        skills = [s.strip() for s in form.get('skills', '').split(',') if s.strip()]
        bio_input = sanitize_input(form.get('bio', '').strip())
        
        if not bio_input:
            bio = generate_bio(
                sanitize_input(form['name']), 
                sanitize_input(form['profession']), 
                ', '.join(skills)
            )
        else:
            bio = bio_input

        # --- Step 6: Process Multiple Experiences ---
        experiences = []
        for key in form.keys():
            if key.startswith('experiences[') and key.endswith('[job_title]'):
                idx = key.split('[')[1].split(']')[0]
                job_title = sanitize_input(form.get(f'experiences[{idx}][job_title]', '').strip())
                company = sanitize_input(form.get(f'experiences[{idx}][company]', '').strip())
                job_desc = sanitize_input(form.get(f'experiences[{idx}][job_desc]', '').strip())
                
                if job_title or company:
                    experiences.append({
                        'job_title': job_title,
                        'company': company,
                        'job_desc': job_desc
                    })

        # --- Step 7: Process Projects ---
        projects = []
        for key in form.keys():
            if key.startswith('projects[') and key.endswith('[name]'):
                idx = key.split('[')[1].split(']')[0]
                project_name = sanitize_input(form.get(f'projects[{idx}][name]', '').strip())
                project_desc = sanitize_input(form.get(f'projects[{idx}][description]', '').strip())
                project_link = form.get(f'projects[{idx}][link]', '').strip()
                
                if project_name:
                    projects.append({
                        'name': project_name,
                        'description': project_desc,
                        'link': project_link
                    })
        
        # --- Step 8: Process Certifications ---
        certifications = []
        for key in form.keys():
            if key.startswith('certifications[') and key.endswith('[name]'):
                idx = key.split('[')[1].split(']')[0]
                cert_name = sanitize_input(form.get(f'certifications[{idx}][name]', '').strip())
                cert_issuer = sanitize_input(form.get(f'certifications[{idx}][issuer]', '').strip())
                cert_year = form.get(f'certifications[{idx}][year]', '').strip()
                
                if cert_name:
                    certifications.append({
                        'name': cert_name,
                        'issuer': cert_issuer,
                        'year': cert_year
                    })

        # --- Step 9: Save Resume to DB ---
        resume = Resume(
            user_id=current_user.id,
            name=sanitize_input(form['name']),
            profession=sanitize_input(form['profession']),
            email=form.get('email', ''),
            phone=form.get('phone', ''),
            linkedin=form.get('linkedin', ''),
            github=form.get('github', ''),
            bio=bio,
            skills=','.join(skills),
            experiences=json.dumps(experiences) if experiences else None,
            degree=sanitize_input(form.get('degree', '')),
            institute=sanitize_input(form.get('institute', '')),
            grad_year=form.get('grad_year', ''),
            profile_pic_url=pic_url,
            template=template,
            projects=json.dumps(projects) if projects else None,
            certifications=json.dumps(certifications) if certifications else None
        )
        db.session.add(resume)

        # --- Step 10: Deduct token & commit ---
        current_user.deduct_token()
        current_user.last_generated = datetime.utcnow()
        db.session.commit()

        # --- Step 11: Render resume directly ---
        context = {
            'name': resume.name,
            'profession': resume.profession,
            'email': resume.email,
            'phone': resume.phone,
            'linkedin': resume.linkedin,
            'github': resume.github,
            'bio': resume.bio,
            'skills': skills,
            'experiences': experiences,
            'degree': resume.degree,
            'institute': resume.institute,
            'grad_year': resume.grad_year,
            'profile_pic_url': resume.profile_pic_url,
            'projects': projects,
            'certifications': certifications
        }

        flash("Resume generated successfully!", "success")
        return render_template(f"resume_{template}.html", **context)
        
    except KeyError as e:
        # Missing required field
        print(f"[ERROR] Resume generation failed: {str(e)}")
        db.session.rollback()
        flash("Failed to generate resume. Please try again.", "error")
        return redirect(url_for('start'))
        
    except Exception as e:
        # Catch any other errors
        db.session.rollback()
        print(f"[ERROR] Resume generation failed: {str(e)}")
        flash("Failed to generate resume. Please try again.", "error")
        return redirect(url_for('start'))

@app.route('/my-resumes')
@login_required
def my_resumes():
    resumes = Resume.query.filter_by(user_id=current_user.id).order_by(Resume.timestamp.desc()).all()
    return render_template('my_resumes.html', resumes=resumes)

@app.route('/resume/<int:resume_id>')
@login_required
def view_resume(resume_id):
    resume = Resume.query.filter_by(id=resume_id, user_id=current_user.id).first_or_404()
    
    # Parse JSON fields
    experiences = json.loads(resume.experiences) if resume.experiences else []
    projects = json.loads(resume.projects) if resume.projects else []
    certifications = json.loads(resume.certifications) if resume.certifications else []
    
    context = {
        'name': resume.name,
        'profession': resume.profession,
        'email': resume.email,
        'phone': resume.phone,
        'linkedin': resume.linkedin,
        'github': resume.github,
        'bio': resume.bio,
        'skills': [s.strip() for s in resume.skills.split(',') if s.strip()],
        'experiences': experiences,
        'degree': resume.degree,
        'institute': resume.institute,
        'grad_year': resume.grad_year,
        'profile_pic_url': resume.profile_pic_url,
        'projects': projects,
        'certifications': certifications
    }
    return render_template(f"resume_{resume.template}.html", **context)

@app.route('/delete_resume/<int:resume_id>', methods=['POST'])
@login_required
def delete_resume(resume_id):
    print(f"[DEBUG] Delete resume called with ID: {resume_id}")
    print(f"[DEBUG] Current user ID: {current_user.id}")
    
    resume = Resume.query.get_or_404(resume_id)
    print(f"[DEBUG] Found resume: {resume.name} owned by user {resume.user_id}")
    
    if resume.user_id != current_user.id:
        flash("Unauthorized", "error")
        return redirect(url_for('start'))

    db.session.delete(resume)
    db.session.commit()
    flash("Resume deleted successfully!", "success")
    print(f"[DEBUG] Resume {resume_id} deleted successfully")
    return redirect(url_for('my_resumes'))


@app.route('/resume/<int:resume_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_resume(resume_id):
    try:
        # Verify ownership
        original = Resume.query.filter_by(id=resume_id, user_id=current_user.id).first_or_404()

        # Handle duplication
        if request.args.get('duplicate') == '1':
            duplicate = Resume(
                user_id=current_user.id,
                name=original.name,
                profession=original.profession,
                email=original.email,
                phone=original.phone,
                linkedin=original.linkedin,
                github=original.github,
                bio=original.bio,
                skills=original.skills,
                experiences=original.experiences,
                degree=original.degree,
                institute=original.institute,
                grad_year=original.grad_year,
                profile_pic_url=original.profile_pic_url,
                template=original.template,
                projects=original.projects,
                certifications=original.certifications
            )
            db.session.add(duplicate)
            db.session.commit()
            flash("Resume duplicated. You can now edit it.", "info")
            return redirect(url_for('edit_resume', resume_id=duplicate.id))

        # Handle POST (Save changes)
        if request.method == 'POST':
            form = request.form
            
            # Update basic fields with sanitization
            original.name = sanitize_input(form.get('name', ''))
            original.profession = sanitize_input(form.get('profession', ''))
            original.email = form.get('email', '')
            original.phone = form.get('phone', '')
            original.linkedin = form.get('linkedin', '')
            original.github = form.get('github', '')
            original.bio = sanitize_input(form.get('bio', ''))
            original.skills = form.get('skills', '')
            original.degree = sanitize_input(form.get('degree', ''))
            original.institute = sanitize_input(form.get('institute', ''))
            original.grad_year = form.get('grad_year', '')
            original.template = form.get('template', 'classic')

            # Handle profile picture upload with validation
            if 'profile_pic' in request.files:
                pic = request.files['profile_pic']
                if pic and pic.filename:
                    # Validate file type
                    allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
                    file_ext = pic.filename.rsplit('.', 1)[1].lower() if '.' in pic.filename else ''
                    
                    if file_ext not in allowed_extensions:
                        flash("Invalid file type. Please upload an image (PNG, JPG, JPEG, GIF, WEBP).", "error")
                        return redirect(url_for('edit_resume', resume_id=resume_id))
                    
                    # Limit file size (5MB)
                    pic.seek(0, os.SEEK_END)
                    file_size = pic.tell()
                    pic.seek(0)
                    
                    if file_size > 5 * 1024 * 1024:  # 5MB
                        flash("File too large. Maximum size is 5MB.", "error")
                        return redirect(url_for('edit_resume', resume_id=resume_id))
                    
                    # Delete old profile picture if exists
                    if original.profile_pic_url:
                        old_path = original.profile_pic_url.replace('/static/uploads/', '')
                        old_filepath = os.path.join(app.config['UPLOAD_FOLDER'], old_path)
                        if os.path.exists(old_filepath):
                            try:
                                os.remove(old_filepath)
                            except Exception as e:
                                print(f"[WARNING] Could not delete old profile pic: {str(e)}")
                    
                    # Save new picture with timestamp
                    filename = secure_filename(pic.filename)
                    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
                    filename = f"{timestamp}_{filename}"
                    path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    pic.save(path)
                    original.profile_pic_url = f"/static/uploads/{filename}"

            # Process Multiple Experiences with sanitization
            experiences = []
            for key in form.keys():
                if key.startswith('experiences[') and key.endswith('[job_title]'):
                    idx = key.split('[')[1].split(']')[0]
                    job_title = sanitize_input(form.get(f'experiences[{idx}][job_title]', '').strip())
                    company = sanitize_input(form.get(f'experiences[{idx}][company]', '').strip())
                    job_desc = sanitize_input(form.get(f'experiences[{idx}][job_desc]', '').strip())
                    
                    if job_title or company:
                        experiences.append({
                            'job_title': job_title,
                            'company': company,
                            'job_desc': job_desc
                        })
            original.experiences = json.dumps(experiences) if experiences else None

            # Process Projects with sanitization
            projects = []
            for key in form.keys():
                if key.startswith('projects[') and key.endswith('[name]'):
                    idx = key.split('[')[1].split(']')[0]
                    project_name = sanitize_input(form.get(f'projects[{idx}][name]', '').strip())
                    project_desc = sanitize_input(form.get(f'projects[{idx}][description]', '').strip())
                    project_link = form.get(f'projects[{idx}][link]', '').strip()
                    
                    if project_name:
                        projects.append({
                            'name': project_name,
                            'description': project_desc,
                            'link': project_link
                        })
            original.projects = json.dumps(projects) if projects else None

            # Process Certifications with sanitization
            certifications = []
            for key in form.keys():
                if key.startswith('certifications[') and key.endswith('[name]'):
                    idx = key.split('[')[1].split(']')[0]
                    cert_name = sanitize_input(form.get(f'certifications[{idx}][name]', '').strip())
                    cert_issuer = sanitize_input(form.get(f'certifications[{idx}][issuer]', '').strip())
                    cert_year = form.get(f'certifications[{idx}][year]', '').strip()
                    
                    if cert_name:
                        certifications.append({
                            'name': cert_name,
                            'issuer': cert_issuer,
                            'year': cert_year
                        })
            original.certifications = json.dumps(certifications) if certifications else None

            db.session.commit()
            flash("Resume updated successfully!", "success")
            return redirect(url_for('my_resumes'))

        # Handle GET (Show edit form)
        # Parse JSON fields for display
        experiences = json.loads(original.experiences) if original.experiences else []
        projects = json.loads(original.projects) if original.projects else []
        certifications = json.loads(original.certifications) if original.certifications else []

        return render_template('edit_resume.html', 
                             resume=original,
                             experiences=experiences,
                             projects=projects,
                             certifications=certifications)
    
    except Exception as e:
        # Handle any errors gracefully
        db.session.rollback()
        print(f"[ERROR] Edit resume failed: {str(e)}")
        flash("Failed to update resume. Please try again.", "error")
        return redirect(url_for('my_resumes'))


@app.route('/buy_token/<int:count>')
@login_required
def buy_token(count):
    price_map = {1: 50, 5: 100}
    amount = price_map.get(count, 0)
    if amount == 0:
        flash("Invalid token pack selected.", "error")
        return redirect(url_for('pricing'))

    current_user.tokens += count
    purchase = Purchase(user_id=current_user.id, amount=amount, description=f"{count} Token Pack")
    db.session.add(purchase)
    db.session.commit()

    flash(f"{count} token{'s' if count > 1 else ''} purchased successfully.", "success")
    return redirect(url_for('pricing'))

@app.route('/upgrade/<string:plan>')
@login_required
def upgrade(plan):
    if plan == 'pro':
        current_user.tokens += 15
        current_user.plan = 'pro'
        purchase = Purchase(user_id=current_user.id, amount=199, description='Pro Pack')
        flash("Upgraded to Pro Pack. 15 tokens added + Premium templates unlocked.", "success")
    elif plan == 'ultimate':
        current_user.tokens = 9999
        current_user.plan = 'ultimate'
        purchase = Purchase(user_id=current_user.id, amount=499, description='Ultimate Pack')
        flash("Welcome to Ultimate Pack. Unlimited tokens + all templates unlocked.", "success")
    else:
        flash("Invalid upgrade option.", "error")
        return redirect(url_for('pricing'))

    db.session.add(purchase)
    db.session.commit()
    return redirect(url_for('my_resumes'))

@app.route('/pricing')
def pricing():
    return render_template('pricing.html')

@app.route('/regen/bio', methods=['POST'])
@login_required
def generate_bio(name, profession, skills):
    """
    Generate a professional bio/summary for resume
    
    Args:
        name: Person's name
        profession: Job title/profession
        skills: Comma-separated skills
    
    Returns:
        Professional bio text
    """
    prompt = f"""Write a compelling professional summary for a resume.

Name: {name}
Role: {profession}
Key Skills: {skills}

Requirements:
- Write in FIRST PERSON (use "I" or "I'm")
- 2-3 sentences maximum
- Highlight unique value proposition
- Mention specific technical skills
- Sound confident but not arrogant
- No generic phrases like "results-driven" or "team player"
- Make it memorable and authentic

Example style: "I'm a Full Stack Developer who transforms complex problems into elegant solutions. With expertise in React, Node.js, and cloud architecture, I build scalable applications that users love. I'm passionate about clean code and mentoring junior developers."

Now write for {name}:"""

    system = """You are an expert resume writer and career coach. 
You write professional summaries that:
- Sound natural and human (not robotic)
- Showcase personality while staying professional
- Use active voice and strong verbs
- Are specific and achievement-focused
- Avoid clichés and buzzwords

Keep it concise, impactful, and authentic."""

    bio = get_ai_response(prompt, system, temperature=0.8, max_tokens=150)
    
    if bio:
        # Clean up the response
        bio = bio.strip()
        
        # Remove quotes if AI added them
        if bio.startswith('"') and bio.endswith('"'):
            bio = bio[1:-1]
        if bio.startswith("'") and bio.endswith("'"):
            bio = bio[1:-1]
        
        return bio
    
    # Fallback bio if AI fails
    return generate_fallback_bio(name, profession, skills)

def generate_fallback_bio(name, profession, skills):
    """
    Generate a simple fallback bio when AI fails
    """
    skills_list = [s.strip() for s in skills.split(',') if s.strip()]
    
    if len(skills_list) >= 3:
        skill_text = f"{skills_list[0]}, {skills_list[1]}, and {skills_list[2]}"
    elif len(skills_list) == 2:
        skill_text = f"{skills_list[0]} and {skills_list[1]}"
    elif len(skills_list) == 1:
        skill_text = skills_list[0]
    else:
        skill_text = "various technologies"
    
    templates = [
        f"I'm {name}, a {profession} with expertise in {skill_text}. I'm passionate about creating impactful solutions and continuously learning new technologies.",
        f"I'm a {profession} specializing in {skill_text}. I combine technical expertise with creative problem-solving to deliver high-quality results.",
        f"As a {profession}, I leverage {skill_text} to build innovative solutions. I'm committed to writing clean code and collaborating effectively with teams."
    ]
    
    import random
    return random.choice(templates)

@app.route('/resume/<int:resume_id>/download', methods=['POST'])
@login_required
def download_resume(resume_id):
    resume = Resume.query.get_or_404(resume_id)
    if resume.user_id != current_user.id:
        flash("Access denied.", "error")
        return redirect(url_for('my_resumes'))

    context = {
        'name': resume.name,
        'profession': resume.profession,
        'email': resume.email,
        'phone': resume.phone,
        'linkedin': resume.linkedin,
        'bio': resume.bio,
        'skills': [s.strip() for s in resume.skills.split(',')],
        'job_title': resume.job_title,
        'company': resume.company,
        'job_desc': resume.job_desc,
        'degree': resume.degree,
        'institute': resume.institute,
        'grad_year': resume.grad_year,
        'profile_pic_url': resume.profile_pic_url
    }

    html = render_template(f"resume_{resume.template}.html", **context)
    folder = f"temp_resume_{resume_id}"
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "resume.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

    return "Resume HTML saved."  # Optional response

def generate_job_description(job_title, company, basic_desc=""):
    """
    Enhance job description with AI (optional feature)
    
    Args:
        job_title: Job position
        company: Company name
        basic_desc: User's initial description
    
    Returns:
        Enhanced job description
    """
    if not basic_desc or len(basic_desc) < 20:
        return basic_desc
    
    prompt = f"""Enhance this job description to be more impactful and achievement-focused.

Job Title: {job_title}
Company: {company}
Original: {basic_desc}

Requirements:
- Keep it concise (2-3 bullet points or sentences)
- Start with strong action verbs
- Quantify achievements where possible
- Focus on impact and results
- Remove fluff and redundancy
- Maintain the original facts

Enhanced version:"""

    system = """You are a resume optimization expert. 
You rewrite job descriptions to:
- Highlight achievements over responsibilities
- Use powerful action verbs
- Be specific and measurable
- Show business impact
- Stay truthful to original content"""

    enhanced = get_ai_response(prompt, system, temperature=0.7, max_tokens=200)
    
    return enhanced if enhanced else basic_desc


def test_ai_connection():
    """
    Test if OpenRouter API is working
    Call this during app startup
    """
    try:
        test_response = get_ai_response(
            "Say 'API Connected' in 2 words",
            "You are a helpful assistant",
            temperature=0.5,
            max_tokens=10
        )
        
        if test_response:
            print("✅ [AI] OpenRouter API connected successfully")
            return True
        else:
            print("❌ [AI] Failed to connect to OpenRouter API")
            return False
            
    except Exception as e:
        print(f"❌ [AI] Connection test failed: {str(e)}")
        return False
# ==============================
# 🔐 Auth Routes
# ==============================

@app.route('/register', methods=['GET', 'POST'])
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        if User.query.filter_by(email=form.email.data).first():
            flash("Email already registered.", "error")
            return redirect(url_for('register'))

        username = f"{form.first_name.data.lower()}.{form.last_name.data.lower()}"
        hashed_pw = generate_password_hash(form.password.data)

        new_user = User(
            username=username,
            first_name=form.first_name.data,
            last_name=form.last_name.data,
            email=form.email.data,
            password=hashed_pw,
            tokens=3,
            plan='free'
        )

        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        flash("Account created successfully!", "success")
        return redirect(url_for('index'))

    return render_template("register.html", form=form)

@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and check_password_hash(user.password, form.password.data):
            login_user(user)
            flash('Logged in successfully.', 'success')
            return redirect(url_for('index'))
        flash('Invalid email or password.', 'danger')
    return render_template('login.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Logged out successfully.", "success")
    return redirect(url_for('login'))

#ERROR HANDLING
@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors"""
    flash("Page not found.", "error")
    return render_template("index.html"), 404


@app.errorhandler(403)
def forbidden_error(error):
    """Handle 403 forbidden errors"""
    flash("You don't have permission to access this resource.", "error")
    return redirect(url_for('index'))


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 internal server errors"""
    db.session.rollback()  # Rollback any failed transactions
    flash("Something went wrong. Please try again later.", "error")
    return render_template("index.html"), 500

with app.app_context():
    test_ai_connection()
# ==============================
# 🚀 Run Server
# ==============================
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
