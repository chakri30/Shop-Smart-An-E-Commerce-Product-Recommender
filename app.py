from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Email, Length, EqualTo
from werkzeug.security import generate_password_hash, check_password_hash
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import os
from dotenv import load_dotenv

# Load .env for local development (do not commit .env to source control)
load_dotenv()
import secrets
from authlib.integrations.flask_client import OAuth

# === APP SETUP ===
app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key-12345'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'  # DB IN ROOT
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# === MODELS ===
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(id):
    return User.query.get(int(id))

# === FORMS ===
class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=20)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField('Register')

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember = BooleanField('Remember Me')
    submit = SubmitField('Login')

# === CREATE DB (NO instance FOLDER NEEDED) ===
with app.app_context():
    db.create_all()

# === LOAD DATA (SMALL SAMPLE TO AVOID MEMORY CRASH) ===
try:
    df_full = pd.read_csv('Online Retail.csv')
    df_full = df_full.dropna(subset=['Description'])
    df_full['Description'] = df_full['Description'].str.lower()
    # Take only 5000 rows to avoid memory crash
    df = df_full.sample(n=min(5000, len(df_full)), random_state=42).reset_index(drop=True)
except Exception as e:
    print("CSV Error:", e)
    # Fallback dummy data
    df = pd.DataFrame({
        'Description': [
            'heart decoration', 'white hanging heart', 'red heart cushion',
            'pack of 72 skull cake cases', 'jumbo bag red retrospot',
            'lunch bag red retrospot', 'set of 3 cake tins',
            'paper chain kit vintage christmas', 'alarm clock bakelike red',
            'gin + tonic diet metal sign'
        ] * 500
    })

# TF-IDF on small dataset
tfidf = TfidfVectorizer(stop_words='english')
tfidf_matrix = tfidf.fit_transform(df['Description'])


# === IMAGE HELPER ===
def get_image_for_description(description, w=300, h=200):
    """Return a visually-pleasing image URL for a product description.
    Tries Unsplash's source endpoint (no API key) and falls back to a text
    placeholder when necessary.
    """
    # Prefer local images for common keywords (offline-friendly)
    keyword = (description or '').lower()
    local_map = {
        'heart': 'img/products/heart.svg',
        'bag': 'img/products/bag.svg',
        'alarm': 'img/products/clock.svg',
        'clock': 'img/products/clock.svg',
        'cake': 'img/products/cake.svg',
        'gin': 'img/products/gin.svg',
        'cushion': 'img/products/cushion.svg',
        'lunch': 'img/products/bag.svg',
        'retrospot': 'img/products/bag.svg'
    }
    for k, path in local_map.items():
        if k in keyword:
            try:
                return url_for('static', filename=path)
            except RuntimeError:
                # url_for must be called with app context; fall back to simple path
                return f'/static/{path}'

    try:
        # use first significant word as keyword for Unsplash
        key = description.split()[0]
        return f'https://source.unsplash.com/featured/{w}x{h}/?{key}'
    except Exception:
        from urllib.parse import quote_plus
        return f'https://via.placeholder.com/{w}x{h}?text={quote_plus(description[:30])}'


# === HELPER FUNCTIONS ===
def get_popular_products():
    popular = df['Description'].value_counts().head(12).index
    return [{'description': d.title(), 'image': get_image_for_description(d)} for d in popular]

def search_products(query):
    query = query.lower()
    results = df[df['Description'].str.contains(query, case=False, na=False)]
    products = results['Description'].unique()[:12]
    return [{'description': p.title(), 'image': get_image_for_description(p)} for p in products]

def get_recommendations(item_desc, top_n=5):
    item_desc = item_desc.lower()
    try:
        item_vec = tfidf.transform([item_desc])
        sim_scores = cosine_similarity(item_vec, tfidf_matrix).flatten()
        # Exclude the item itself
        indices = sim_scores.argsort()[-(top_n+5):][::-1]
        recs = []
        for idx in indices:
            desc = df['Description'].iloc[idx]
            if desc.lower() != item_desc and desc not in recs:
                recs.append(desc.title())
            if len(recs) >= top_n:
                break
        return recs
    except:
        return ["Heart Decoration", "Red Cushion", "Gift Bag"]


def generate_explanation(user_id, item_desc, recommended_items):
    if not recommended_items:
        return f"Because you searched '{item_desc}', here are popular alternatives."
    
    # Use first 3 recommended items
    recs = recommended_items[:3]
    prompt = (
        f"User {user_id} searched for '{item_desc}'. "
        f"Based on similarity, recommend: {', '.join(recs)}. "
        f"Explain in 1 short sentence why these are good matches."
    )
    
    try:
        # Lazy-import the transformers pipeline to avoid import-time failures when
        # the heavy dependencies (transformers / torch) are not installed.
        try:
            from transformers import pipeline as _pipeline
        except Exception as ie:
            # transformers not available; return a simple fallback explanation
            print('Transformers import skipped:', ie)
            return f"Similar to '{item_desc}', try: {', '.join(recs)}."

        try:
            generator = _pipeline('text-generation', model='distilgpt2', max_length=80, truncation=True)
        except Exception as ie:
            # Model loading failed (no weights or backend); fallback
            print('LLM pipeline init failed:', ie)
            return f"Similar to '{item_desc}', try: {', '.join(recs)}."

        result = generator(
            prompt,
            max_length=80,
            num_return_sequences=1,
            truncation=True,
            temperature=0.7,
            do_sample=True
        )[0]
        text = result.get('generated_text', '')
        # Clean output
        explanation = text.split("Explain")[0].strip()
        if len(explanation) < 10:
            return f"These items match your interest in '{item_desc}'."
        return explanation
    except Exception as e:
        print("LLM Error:", e)
        return f"Similar to '{item_desc}', try: {', '.join(recs)}."

# === ROUTES ===

# === OAUTH SETUP ===
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')
GITHUB_CLIENT_ID = os.environ.get('GITHUB_CLIENT_ID')
GITHUB_CLIENT_SECRET = os.environ.get('GITHUB_CLIENT_SECRET')

oauth = OAuth(app)
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name='google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'}
    )

if GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET:
    oauth.register(
        name='github',
        client_id=GITHUB_CLIENT_ID,
        client_secret=GITHUB_CLIENT_SECRET,
        access_token_url='https://github.com/login/oauth/access_token',
        authorize_url='https://github.com/login/oauth/authorize',
        api_base_url='https://api.github.com/',
        client_kwargs={'scope': 'user:email'}
    )


def ensure_oauth_registered(provider):
    """Ensure the OAuth client for provider is registered with authlib.
    This reads environment variables at runtime so changes to .env or env
    in a running session are respected when possible.
    Returns True if registered, False if credentials missing.
    """
    provider = provider.lower()
    try:
        # If already registered, nothing to do
        _ = oauth.create_client(provider)
        return True
    except Exception:
        # Try to register from current environment variables
        if provider == 'google':
            cid = os.environ.get('GOOGLE_CLIENT_ID')
            secret = os.environ.get('GOOGLE_CLIENT_SECRET')
            if cid and secret:
                oauth.register(
                    name='google',
                    client_id=cid,
                    client_secret=secret,
                    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
                    client_kwargs={'scope': 'openid email profile'}
                )
                return True
            return False
        elif provider == 'github':
            cid = os.environ.get('GITHUB_CLIENT_ID')
            secret = os.environ.get('GITHUB_CLIENT_SECRET')
            if cid and secret:
                oauth.register(
                    name='github',
                    client_id=cid,
                    client_secret=secret,
                    access_token_url='https://github.com/login/oauth/access_token',
                    authorize_url='https://github.com/login/oauth/authorize',
                    api_base_url='https://api.github.com/',
                    client_kwargs={'scope': 'user:email'}
                )
                return True
            return False
        return False


@app.route('/login/<provider>')
def oauth_login(provider):
    """Start OAuth flow for provider (google or github). If credentials are not
    configured, flash a helpful message and redirect back to register page.
    """
    provider = provider.lower()
    if provider not in ('google', 'github'):
        flash('Unknown provider.', 'danger')
        return redirect(url_for('register'))

    # Ensure client configured (check current environment and try to register if possible)
    ok = ensure_oauth_registered(provider)
    if not ok:
        flash(f'{provider.capitalize()} OAuth not configured. Set {provider.upper()}_CLIENT_ID and {provider.upper()}_CLIENT_SECRET.', 'warning')
        return redirect(url_for('register'))

    redirect_uri = url_for('oauth_callback', provider=provider, _external=True)
    try:
        return oauth.create_client(provider).authorize_redirect(redirect_uri)
    except Exception as e:
        # Fallback: show helpful message
        print('OAuth authorize error:', e)
        flash(f'Unable to start {provider} OAuth flow. See server logs.', 'danger')
        return redirect(url_for('register'))


@app.route('/auth/<provider>/callback')
def oauth_callback(provider):
    provider = provider.lower()
    if provider not in ('google', 'github'):
        flash('Unknown provider.', 'danger')
        return redirect(url_for('login'))

    # Ensure client is registered (in case env changed while server running)
    if not ensure_oauth_registered(provider):
        flash(f'{provider.capitalize()} OAuth not configured. Set {provider.upper()}_CLIENT_ID and {provider.upper()}_CLIENT_SECRET.', 'warning')
        return redirect(url_for('login'))

    try:
        client = oauth.create_client(provider)
    except Exception as e:
        print('OAuth client creation failed:', e)
        flash('OAuth client initialization failed. See server logs.', 'danger')
        return redirect(url_for('login'))

    token = client.authorize_access_token()

    # Extract user info depending on provider
    email = None
    name = None
    try:
        if provider == 'google':
            userinfo = client.parse_id_token(token)
            email = userinfo.get('email')
            name = userinfo.get('name') or (email.split('@')[0] if email else None)
        else:  # github
            resp = client.get('user')
            profile = resp.json()
            email = profile.get('email')
            name = profile.get('name') or profile.get('login')
            if not email:
                # fetch emails list
                resp2 = client.get('user/emails')
                emails = resp2.json() if resp2.ok else []
                primary = next((e['email'] for e in emails if e.get('primary') and e.get('verified')), None)
                email = primary or (emails[0]['email'] if emails else None)
    except Exception as e:
        print('OAuth callback parsing error:', e)

    if not email:
        flash('Could not retrieve email from provider. Make sure your account shares email.', 'danger')
        return redirect(url_for('login'))

    # Find or create user
    user = User.query.filter_by(email=email).first()
    if not user:
        username = (name or email.split('@')[0]).replace(' ', '_')[:20]
        base = username
        i = 1
        while User.query.filter_by(username=username).first():
            username = f"{base}{i}"
            i += 1
        user = User(username=username, email=email)
        # assign a random password (not used)
        user.set_password(secrets.token_urlsafe(16))
        db.session.add(user)
        db.session.commit()

    login_user(user)
    flash(f'Logged in as {user.username} via {provider.capitalize()}', 'success')
    return redirect(url_for('main'))
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main'))
    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(username=form.username.data, email=form.email.data)
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        flash('Registration successful!', 'success')
        return redirect(url_for('login'))
    return render_template('register.html', form=form)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            flash(f'Welcome, {user.username}!', 'success')
            return redirect(url_for('main'))
        flash('Invalid email or password', 'danger')
    return render_template('login.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/main')
@login_required
def main():
    query = request.args.get('q', '').strip()
    products = search_products(query) if query else get_popular_products()
    return render_template('main.html', products=products, query=query)

@app.route('/api/recommendations/<int:user_id>/<path:item>')
def api_recommendations(user_id, item):
    recs = get_recommendations(item)
    explanation = generate_explanation(user_id, item, recs)
    return jsonify({
        'user_id': user_id,
        'searched_item': item,
        'recommended': recs,
        'explanation': explanation
    })

# === RUN ===
if __name__ == '__main__':
    app.run(debug=True)