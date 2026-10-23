import os
import hmac
import ipaddress
import math
import sqlite3
import uuid
import secrets
from datetime import date
from urllib.parse import quote
from flask import Flask, abort, flash, render_template, request, redirect, url_for, send_file, send_from_directory, session
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from markupsafe import Markup
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

app = Flask(__name__, template_folder='.')
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(32)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['DATABASE'] = 'database.db'
app.config['MAX_CONTENT_LENGTH'] = 75 * 1024 * 1024
app.config['ADMIN_PASSWORD'] = os.environ.get('SIDDA_ADMIN_PASSWORD')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SIDDA_HTTPS') == '1'

APPROVAL_DEFAULTS = (
    ('Mother Deed', 'Pending'),
    ('Encumbrance Certificate (EC)', 'Pending'),
    ('A & B Khata Certificate', 'Pending'),
    ('Property Tax Receipts', 'Pending'),
)
LEGACY_DOCUMENT_NAMES = {
    'EC': 'Encumbrance Certificate (EC)',
    'Khata': 'A & B Khata Certificate',
    'Property Tax': 'Property Tax Receipts',
}
PROPERTY_DOCUMENT_STEPS = (
    ('sale-deed', 'site detail', 'Sale Deed'),
    ('ec', 'approval', 'Encumbrance Certificate (EC)'),
    ('khata', 'approval', 'A & B Khata Certificate'),
    ('property-tax', 'approval', 'Property Tax Receipts'),
)
ALLOWED_DOCUMENT_EXTENSIONS = {'.pdf'}
MEDIA_EXTENSIONS = {
    'photo': {'.jpg', '.jpeg', '.png', '.webp', '.mp4', '.webm'},
    'map': {'.jpg', '.jpeg', '.png', '.webp'},
    '3d': {'.glb'},
    'blueprint': {'.pdf', '.jpg', '.jpeg', '.png', '.webp'},
}
MEDIA_MAX_BYTES = 50 * 1024 * 1024
DOCUMENT_MAX_BYTES = 10 * 1024 * 1024
SITE_DETAIL_FIELDS = (
    'Sale Deed',
)
CONSTRUCTION_FLOORS = ('Ground Floor',) + tuple(
    f'Floor {floor}' for floor in range(1, 11)
)
FLOOR_BUDGET_CATEGORIES = (
    'Structure',
    'Walls & plaster',
    'Flooring',
    'Electrical',
    'Plumbing',
    'Doors & windows',
    'Painting & finishing',
    'Other',
)
COMMON_BUDGET_CATEGORIES = (
    'Lift',
    'Staircase',
    'Water storage',
    'Fire safety',
    'Approvals & fees',
    'Other',
)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def get_db():
    conn = sqlite3.connect(app.config['DATABASE'])
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name TEXT NOT NULL,
                project_name TEXT NOT NULL,
                budget REAL NOT NULL,
                status TEXT NOT NULL,
                filename TEXT,
                approval_status TEXT NOT NULL DEFAULT 'Not Applied'
            )
        ''')
        project_columns = [
            column['name'] for column in conn.execute('PRAGMA table_info(projects)').fetchall()
        ]
        if 'approval_status' not in project_columns:
            conn.execute(
                "ALTER TABLE projects ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'Not Applied'"
            )
        for column_name, definition in (
            ('description', "TEXT NOT NULL DEFAULT ''"),
            ('location', "TEXT NOT NULL DEFAULT ''"),
            ('is_published', 'INTEGER NOT NULL DEFAULT 0'),
        ):
            if column_name not in project_columns:
                conn.execute(
                    f'ALTER TABLE projects ADD COLUMN {column_name} {definition}'
                )
        conn.execute(
            "UPDATE projects SET approval_status = 'Not Applied' WHERE approval_status IS NULL"
        )
        conn.execute('''
            CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                filename TEXT,
                notes TEXT NOT NULL DEFAULT '',
                UNIQUE(project_id, name),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS site_details (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Pending',
                filename TEXT,
                UNIQUE(project_id, name),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS project_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                filename TEXT NOT NULL,
                original_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS quote_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                area_sqft INTEGER NOT NULL,
                service TEXT NOT NULL,
                budget_estimate REAL NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS admin_credentials (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                password_hash TEXT NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS construction_budget_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                section TEXT NOT NULL,
                category TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                UNIQUE(project_id, section, category),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS construction_schedules (
                project_id INTEGER PRIMARY KEY,
                duration_months INTEGER NOT NULL DEFAULT 24,
                start_date TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS customer_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                customer_name TEXT NOT NULL,
                completed_work TEXT NOT NULL,
                amount_received REAL NOT NULL,
                payment_date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        ''')
        projects = conn.execute('SELECT id FROM projects').fetchall()
        for project in projects:
            ensure_project_approvals(conn, project['id'])
            ensure_project_site_details(conn, project['id'])
            migrate_legacy_documents(conn, project['id'])
        conn.commit()
    finally:
        conn.close()

def ensure_project_approvals(conn, project_id):
    conn.executemany(
        'INSERT OR IGNORE INTO approvals (project_id, name, status) VALUES (?, ?, ?)',
        [(project_id, name, status) for name, status in APPROVAL_DEFAULTS]
    )

def ensure_project_site_details(conn, project_id):
    conn.executemany(
        'INSERT OR IGNORE INTO site_details (project_id, name) VALUES (?, ?)',
        [(project_id, name) for name in SITE_DETAIL_FIELDS]
    )

def migrate_legacy_documents(conn, project_id):
    for old_name, new_name in LEGACY_DOCUMENT_NAMES.items():
        old_document = conn.execute(
            'SELECT * FROM approvals WHERE project_id = ? AND name = ?',
            (project_id, old_name)
        ).fetchone()
        new_document = conn.execute(
            'SELECT * FROM approvals WHERE project_id = ? AND name = ?',
            (project_id, new_name)
        ).fetchone()
        if old_document and new_document:
            if not new_document['filename']:
                conn.execute(
                    'UPDATE approvals SET status = ?, filename = ?, notes = ? WHERE id = ?',
                    (old_document['status'], old_document['filename'],
                     old_document['notes'], new_document['id'])
                )
            conn.execute(
                'UPDATE approvals SET filename = NULL WHERE id = ?',
                (old_document['id'],)
            )

    combined_khata = conn.execute(
        "SELECT * FROM approvals WHERE project_id = ? AND name = 'A & B Khata Certificate'",
        (project_id,)
    ).fetchone()
    if combined_khata and not combined_khata['filename']:
        khata_documents = conn.execute(
            """SELECT * FROM approvals
               WHERE project_id = ? AND name IN ('A Khata Certificate', 'B Khata Certificate')
               ORDER BY CASE name WHEN 'A Khata Certificate' THEN 0 ELSE 1 END""",
            (project_id,)
        ).fetchall()
        source = next(
            (document for document in khata_documents if document['filename']),
            next(
                (document for document in khata_documents if document['status'] != 'Pending'),
                None
            )
        )
        if source:
            conn.execute(
                'UPDATE approvals SET status = ?, filename = ?, notes = ? WHERE id = ?',
                (source['status'], source['filename'], source['notes'], combined_khata['id'])
            )

    old_sale_deed = conn.execute(
        "SELECT * FROM approvals WHERE project_id = ? AND name = 'Sale Deed'",
        (project_id,)
    ).fetchone()
    site_sale_deed = conn.execute(
        "SELECT * FROM site_details WHERE project_id = ? AND name = 'Sale Deed'",
        (project_id,)
    ).fetchone()
    if old_sale_deed and site_sale_deed:
        if not site_sale_deed['filename']:
            conn.execute(
                'UPDATE site_details SET status = ?, filename = ? WHERE id = ?',
                (old_sale_deed['status'], old_sale_deed['filename'], site_sale_deed['id'])
            )
        conn.execute(
            'UPDATE approvals SET filename = NULL WHERE id = ?',
            (old_sale_deed['id'],)
        )

def format_indian_currency(value):
    integer, decimal = f'{float(value):.2f}'.split('.')
    if len(integer) > 3:
        leading, last_three = integer[:-3], integer[-3:]
        groups = []
        while len(leading) > 2:
            groups.insert(0, leading[-2:])
            leading = leading[:-2]
        if leading:
            groups.insert(0, leading)
        integer = ','.join(groups + [last_three])
    return f'{integer}.{decimal}'

app.jinja_env.filters['inr'] = format_indian_currency

def get_approval(conn, approval_id):
    return conn.execute(
        'SELECT * FROM approvals WHERE id = ?', (approval_id,)
    ).fetchone()

def get_site_detail(conn, detail_id):
    return conn.execute(
        'SELECT * FROM site_details WHERE id = ?', (detail_id,)
    ).fetchone()

def project_requires_approval(conn, project_id):
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    if not project:
        return False

    if not project['filename']:
        return False

    sale_deed = conn.execute(
        "SELECT * FROM site_details WHERE project_id = ? AND name = 'Sale Deed'",
        (project_id,)
    ).fetchone()
    if not sale_deed or (
        sale_deed['status'] != 'N/A' and not sale_deed['filename']
    ):
        return False

    approval_names = [name for name, _ in APPROVAL_DEFAULTS]
    approvals = conn.execute(
        f"SELECT * FROM approvals WHERE project_id = ? AND name IN ({', '.join('?' for _ in approval_names)})",
        (project_id, *approval_names)
    ).fetchall()
    if not approvals or any(
        approval['status'] != 'N/A' and (
            approval['status'] != 'Verified' or not approval['filename']
        )
        for approval in approvals
    ):
        return False

    return True

def project_status_summaries(conn, project_ids):
    if not project_ids:
        return {}
    placeholders = ', '.join('?' for _ in project_ids)
    approval_names = [name for name, _ in APPROVAL_DEFAULTS]
    approval_placeholders = ', '.join('?' for _ in approval_names)
    approvals = conn.execute(
        f'''SELECT id, project_id, name, status, filename FROM approvals
            WHERE project_id IN ({placeholders})
              AND name IN ({approval_placeholders}) ORDER BY id''',
        (*project_ids, *approval_names)
    ).fetchall()
    details = conn.execute(
        f'''SELECT id, project_id, name, status, filename FROM site_details
            WHERE project_id IN ({placeholders}) AND name = 'Sale Deed'
            ORDER BY id''',
        project_ids
    ).fetchall()
    summaries = {project_id: [] for project_id in project_ids}
    for row in approvals:
        summaries[row['project_id']].append({
            'name': row['name'], 'status': row['status'],
            'filename': row['filename'], 'kind': 'approval', 'id': row['id']
        })
    for row in details:
        summaries[row['project_id']].append({
            'name': row['name'], 'status': row['status'],
            'filename': row['filename'], 'kind': 'site detail', 'id': row['id']
        })
    return summaries

def get_dashboard_projects(conn):
    projects = conn.execute('SELECT * FROM projects ORDER BY id DESC').fetchall()
    project_ids = [project['id'] for project in projects]
    summaries = project_status_summaries(conn, project_ids)
    return projects, summaries

def ensure_construction_plan(conn, project_id):
    budget_items = [
        (project_id, floor, category)
        for floor in CONSTRUCTION_FLOORS
        for category in FLOOR_BUDGET_CATEGORIES
    ] + [
        (project_id, 'Common', category)
        for category in COMMON_BUDGET_CATEGORIES
    ]
    conn.executemany(
        '''INSERT OR IGNORE INTO construction_budget_items
           (project_id, section, category) VALUES (?, ?, ?)''',
        budget_items
    )
    conn.execute(
        '''INSERT OR IGNORE INTO construction_schedules (project_id)
           VALUES (?)''',
        (project_id,)
    )

def create_quote_request(form):
    name = form.get('name', '').strip()
    phone = form.get('phone', '').strip()
    email = form.get('email', '').strip()
    location = form.get('location', '').strip()
    service = form.get('service', '').strip()
    message = form.get('message', '').strip()
    area_raw = form.get('area_sqft', '')
    rate_raw = form.get('rate_per_sqft', '')

    if not name or len(name) > 100:
        raise ValueError('Enter your name (100 characters or fewer).')
    if not phone or len(phone) > 30:
        raise ValueError('Enter a phone number where we can reach you.')
    if len(email) > 254 or (email and ('@' not in email or any(char.isspace() for char in email))):
        raise ValueError('Enter a valid email address, or leave it blank.')
    if len(location) > 300 or len(message) > 2000:
        raise ValueError('Keep the location under 300 characters and the message under 2,000.')
    if service not in {'New home construction', 'Renovation', 'Design & floor plans', 'Commercial construction'}:
        raise ValueError('Choose a service from the list.')
    try:
        area = int(area_raw)
        rate = float(rate_raw)
    except (TypeError, ValueError):
        raise ValueError('Enter a valid floor area and estimate rate.') from None
    if not 100 <= area <= 100000 or not math.isfinite(rate) or rate not in {1800, 2400, 3200}:
        raise ValueError('Floor area must be 100–100,000 sq ft. Choose one of the displayed example rates.')
    if not secrets.compare_digest(form.get('website', ''), ''):
        raise ValueError('The request could not be submitted.')
    return {
        'name': name, 'phone': phone, 'email': email, 'location': location,
        'area': area, 'service': service, 'estimate': area * rate,
        'message': message
    }

def add_quote_request(data):
    conn = get_db()
    try:
        conn.execute(
            '''INSERT INTO quote_requests
               (name, phone, email, location, area_sqft, service, budget_estimate, message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (data['name'], data['phone'], data['email'], data['location'],
             data['area'], data['service'], data['estimate'], data['message'])
        )
        conn.commit()
    finally:
        conn.close()

def validate_pdf_document(file):
    original_name = secure_filename(file.filename or '')
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValueError('Only PDF documents are allowed.')

    file.stream.seek(0, os.SEEK_END)
    file_size = file.stream.tell()
    file.stream.seek(0)
    if file_size > DOCUMENT_MAX_BYTES:
        raise ValueError('PDF documents must be 10 MB or smaller.')
    header = file.stream.read(1024)
    file.stream.seek(0)
    if b'%PDF-' not in header:
        raise ValueError('The selected file is not a valid PDF document.')
    return extension

def save_uploaded_document(file, project_id, document_id):
    extension = validate_pdf_document(file)
    stored_name = f'{project_id}_{document_id}_{uuid.uuid4().hex}{extension}'
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], stored_name))
    return stored_name

def remove_uploaded_document(filename):
    if filename:
        path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.isfile(path):
            os.remove(path)

def csrf_input():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return Markup('<input type="hidden" name="_csrf_token" value="{}">').format(token)

app.jinja_env.globals['csrf_input'] = csrf_input

def is_local_request():
    try:
        return ipaddress.ip_address(request.remote_addr or '').is_loopback
    except ValueError:
        return False

def get_configured_admin_password_hash():
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT password_hash FROM admin_credentials WHERE id = 1'
        ).fetchone()
        return row['password_hash'] if row else None
    finally:
        conn.close()

@app.before_request
def protect_admin_and_forms():
    if request.endpoint == 'static':
        return None
    csrf_input()
    if request.method == 'POST':
        expected = session.get('_csrf_token', '')
        supplied = request.form.get('_csrf_token', '')
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, 'The form expired or could not be verified. Please try again.')

    public_endpoints = {
        'login', 'setup_admin', 'public_portfolio', 'public_project',
        'public_media', 'request_quote'
    }
    if request.endpoint in public_endpoints:
        return None
    if not session.get('admin_authenticated'):
        return redirect(url_for('login', next=request.full_path))
    return None

@app.after_request
def secure_response_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    return response

@app.errorhandler(413)
def uploaded_file_too_large(error):
    flash('The combined upload is too large. Each media file may be up to 50 MB, within a 75 MB request limit.', 'error')
    return redirect(request.referrer or url_for('index'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('admin_authenticated'):
        return redirect(url_for('index'))
    if request.method == 'POST':
        password = request.form.get('password', '')
        configured_password = app.config['ADMIN_PASSWORD']
        password_hash = (
            get_configured_admin_password_hash() if not configured_password else None
        )
        authenticated = (
            hmac.compare_digest(
                password.encode('utf-8'), configured_password.encode('utf-8')
            ) if configured_password else
            bool(password_hash and check_password_hash(password_hash, password))
        )
        if authenticated:
            session.clear()
            session['admin_authenticated'] = True
            csrf_input()
            target = request.args.get('next', '')
            if not target.startswith('/') or target.startswith('//') or '\\' in target:
                target = url_for('index')
            return redirect(target)
        if not app.config['ADMIN_PASSWORD'] and not get_configured_admin_password_hash():
            return render_template(
                'login.html', configuration_error=True,
                setup_available=is_local_request()
            ), 503
        flash('That password was not accepted.', 'error')
    admin_is_configured = bool(
        app.config['ADMIN_PASSWORD'] or get_configured_admin_password_hash()
    )
    return render_template(
        'login.html', configuration_error=not admin_is_configured,
        setup_available=is_local_request() and not admin_is_configured
    )

@app.route('/setup-admin', methods=['GET', 'POST'])
def setup_admin():
    if not is_local_request():
        abort(404)
    if app.config['ADMIN_PASSWORD']:
        return 'Admin setup is disabled because SIDDA_ADMIN_PASSWORD is configured.', 403
    if get_configured_admin_password_hash():
        return 'Admin access has already been set up.', 403

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirmation = request.form.get('confirm_password', '')
        if len(password) < 12:
            flash('Choose a password with at least 12 characters.', 'error')
        elif len(password) > 1024:
            flash('Password must be 1,024 characters or fewer.', 'error')
        elif password != confirmation:
            flash('The passwords do not match.', 'error')
        else:
            conn = get_db()
            cursor = conn.execute(
                '''INSERT OR IGNORE INTO admin_credentials (id, password_hash)
                   VALUES (1, ?)''',
                (generate_password_hash(password),)
            )
            conn.commit()
            conn.close()
            if cursor.rowcount != 1:
                return 'Admin access has already been set up.', 403
            flash('Admin password created. You can now sign in.', 'success')
            return redirect(url_for('login'))

    return render_template('admin_setup.html')

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash('You have been signed out.', 'success')
    return redirect(url_for('login'))

@app.route('/')
@app.route('/portfolio')
def public_portfolio():
    conn = get_db()
    projects = conn.execute(
        """SELECT p.*,
              (SELECT m.id FROM project_media m
               WHERE m.project_id = p.id AND m.category = 'photo'
                 AND (lower(m.filename) LIKE '%.jpg'
                   OR lower(m.filename) LIKE '%.jpeg'
                   OR lower(m.filename) LIKE '%.png'
                   OR lower(m.filename) LIKE '%.webp')
               ORDER BY m.id LIMIT 1) AS cover_media_id
           FROM projects p WHERE p.is_published = 1 ORDER BY p.id DESC"""
    ).fetchall()
    conn.close()
    return render_template('public_portfolio.html', projects=projects)

@app.route('/admin')
def index():
    conn = get_db()
    projects, document_summaries = get_dashboard_projects(conn)
    quotes = conn.execute(
        'SELECT * FROM quote_requests ORDER BY id DESC LIMIT 50'
    ).fetchall()
    conn.close()
    return render_template(
        'index.html', projects=projects, document_summaries=document_summaries,
        quotes=quotes
    )

@app.route('/showcase/<int:project_id>')
def public_project(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ? AND is_published = 1', (project_id,)
    ).fetchone()
    if not project:
        conn.close()
        abort(404)
    media = conn.execute(
        'SELECT * FROM project_media WHERE project_id = ? ORDER BY id',
        (project_id,)
    ).fetchall()
    conn.close()
    return render_template(
        'public_project.html', project=project, media=media,
        map_query=quote(project['location'], safe='')
    )

@app.route('/request-quote', methods=['POST'])
def request_quote():
    try:
        quote_request = create_quote_request(request.form)
    except ValueError as error:
        flash(str(error), 'error')
        return redirect(url_for('public_portfolio') + '#quote')
    add_quote_request(quote_request)
    flash('Thank you! SIDDA Construction has received your request. We will contact you soon.', 'success')
    return redirect(url_for('public_portfolio') + '#quote')

def get_project_media(media_id):
    conn = get_db()
    media = conn.execute(
        '''SELECT m.*, p.is_published FROM project_media m
           JOIN projects p ON p.id = m.project_id WHERE m.id = ?''',
        (media_id,)
    ).fetchone()
    conn.close()
    return media

def send_project_media(media, as_attachment=False):
    extension = os.path.splitext(media['filename'])[1].lower()
    mimetypes = {
        '.glb': 'model/gltf-binary',
        '.pdf': 'application/pdf',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.webp': 'image/webp',
        '.mp4': 'video/mp4',
        '.webm': 'video/webm',
    }
    response = send_from_directory(
        app.config['UPLOAD_FOLDER'], media['filename'],
        as_attachment=as_attachment, download_name=media['original_name'],
        mimetype=mimetypes.get(extension, 'application/octet-stream')
    )
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/public-media/<int:media_id>')
def public_media(media_id):
    media = get_project_media(media_id)
    if not media or not media['is_published']:
        abort(404)
    return send_project_media(media)

@app.route('/admin-media/<int:media_id>')
def admin_media(media_id):
    media = get_project_media(media_id)
    if not media:
        abort(404)
    return send_project_media(media)

@app.route('/projects/<int:project_id>/website', methods=['GET', 'POST'])
def manage_project_website(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    if not project:
        conn.close()
        return 'Project not found', 404
    if request.method == 'POST':
        title = request.form.get('project_name', '').strip()
        description = request.form.get('description', '').strip()
        location = request.form.get('location', '').strip()
        if not title or len(title) > 120 or len(description) > 3000 or len(location) > 300:
            flash('Enter a project name (up to 120 characters), description (up to 3,000), and location (up to 300).', 'error')
        else:
            published = 1 if request.form.get('is_published') == 'on' else 0
            conn.execute(
                '''UPDATE projects SET project_name = ?, description = ?,
                   location = ?, is_published = ? WHERE id = ?''',
                (title, description, location, published, project_id)
            )
            conn.commit()
            flash('Website details saved. ' + ('This project is now public.' if published else 'This project is private.'), 'success')
            if published:
                conn.close()
                return redirect(url_for('manage_project_website', project_id=project_id))
        project = conn.execute(
            'SELECT * FROM projects WHERE id = ?', (project_id,)
        ).fetchone()
    media = conn.execute(
        'SELECT * FROM project_media WHERE project_id = ? ORDER BY id DESC',
        (project_id,)
    ).fetchall()
    conn.close()
    return render_template('manage_website.html', project=project, media=media)

@app.route('/projects/<int:project_id>/media/upload', methods=['POST'])
def upload_project_media(project_id):
    conn = get_db()
    project = conn.execute('SELECT id FROM projects WHERE id = ?', (project_id,)).fetchone()
    if not project:
        conn.close()
        return 'Project not found', 404
    category = request.form.get('category', '')
    if category not in MEDIA_EXTENSIONS:
        conn.close()
        flash('Choose a valid media category.', 'error')
        return redirect(url_for('manage_project_website', project_id=project_id))

    files = [file for file in request.files.getlist('files') if file and file.filename]
    if not files:
        conn.close()
        flash('Choose at least one file to upload.', 'error')
        return redirect(url_for('manage_project_website', project_id=project_id))
    title = request.form.get('title', '').strip()
    if len(title) > 120:
        conn.close()
        flash('Media titles must be 120 characters or fewer.', 'error')
        return redirect(url_for('manage_project_website', project_id=project_id))

    saved_files = []
    try:
        for file in files:
            original_name = secure_filename(file.filename or '')
            extension = os.path.splitext(original_name)[1].lower()
            if extension not in MEDIA_EXTENSIONS[category]:
                raise ValueError(f'{original_name or "This file"} is not supported for this category.')
            content = file.stream.read(MEDIA_MAX_BYTES + 1)
            file.stream.seek(0)
            if len(content) > MEDIA_MAX_BYTES:
                raise ValueError('Each media file must be 50 MB or smaller.')
            if extension in {'.jpg', '.jpeg'} and not content.startswith(b'\xff\xd8\xff'):
                raise ValueError(f'{original_name} is not a valid JPEG image.')
            if extension == '.png' and not content.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError(f'{original_name} is not a valid PNG image.')
            if extension == '.webp' and (len(content) < 12 or content[:4] != b'RIFF' or content[8:12] != b'WEBP'):
                raise ValueError(f'{original_name} is not a valid WebP image.')
            if extension == '.pdf' and b'%PDF-' not in content[:1024]:
                raise ValueError(f'{original_name} is not a valid PDF document.')
            if extension == '.glb' and (
                len(content) < 20 or content[:4] != b'glTF'
                or int.from_bytes(content[4:8], 'little') != 2
                or int.from_bytes(content[8:12], 'little') != len(content)
            ):
                raise ValueError(f'{original_name} is not a valid GLB 2.0 3D model.')
            if extension == '.mp4' and (len(content) < 12 or content[4:8] != b'ftyp'):
                raise ValueError(f'{original_name} is not a valid MP4 video.')
            if extension == '.webm' and not content.startswith(b'\x1a\x45\xdf\xa3'):
                raise ValueError(f'{original_name} is not a valid WebM video.')

            stored_name = f'{project_id}_media_{uuid.uuid4().hex}{extension}'
            saved_files.append(stored_name)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], stored_name))
            display_title = title or os.path.splitext(original_name)[0] or 'Project media'
            conn.execute(
                '''INSERT INTO project_media
                   (project_id, category, title, filename, original_name)
                   VALUES (?, ?, ?, ?, ?)''',
                (project_id, category, display_title, stored_name, original_name)
            )
        conn.commit()
    except ValueError as error:
        conn.rollback()
        for filename in saved_files:
            remove_uploaded_document(filename)
        flash(str(error), 'error')
        conn.close()
        return redirect(url_for('manage_project_website', project_id=project_id))
    except OSError:
        conn.rollback()
        for filename in saved_files:
            remove_uploaded_document(filename)
        conn.close()
        raise
    except sqlite3.Error:
        conn.rollback()
        for filename in saved_files:
            remove_uploaded_document(filename)
        conn.close()
        raise
    conn.close()
    flash(f'{len(files)} file(s) uploaded to the project website.', 'success')
    return redirect(url_for('manage_project_website', project_id=project_id))

@app.route('/project-media/<int:media_id>/delete', methods=['POST'])
def delete_project_media(media_id):
    conn = get_db()
    media = conn.execute(
        'SELECT * FROM project_media WHERE id = ?', (media_id,)
    ).fetchone()
    if not media:
        conn.close()
        return 'Project media not found', 404
    conn.execute('DELETE FROM project_media WHERE id = ?', (media_id,))
    conn.commit()
    conn.close()
    remove_uploaded_document(media['filename'])
    flash('Project media deleted.', 'success')
    return redirect(url_for('manage_project_website', project_id=media['project_id']))

@app.route('/add', methods=['GET', 'POST'])
def add_project():
    if request.method == 'POST':
        client_name = request.form['client_name']
        project_name = request.form['project_name']
        budget = request.form['budget']
        status = request.form['status']
        
        file = request.files.get('file')
        has_file = bool(file and file.filename)
        if has_file:
            try:
                validate_pdf_document(file)
            except ValueError as error:
                flash(str(error), 'error')
                return render_template('project_form.html')
        conn = get_db()
        conn.execute(
            'INSERT INTO projects (client_name, project_name, budget, status, filename) VALUES (?, ?, ?, ?, ?)',
            (client_name, project_name, budget, status, '')
        )
        project_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        ensure_project_approvals(conn, project_id)
        ensure_project_site_details(conn, project_id)
        if has_file:
            filename = save_uploaded_document(file, project_id, 'blueprint')
            conn.execute(
                'UPDATE projects SET filename = ? WHERE id = ?', (filename, project_id)
            )
        conn.commit()
        conn.close()
        return redirect(url_for('index'))
    return render_template('project_form.html')

@app.route('/projects/<int:project_id>/site-details')
def project_site_details(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    if not project:
        conn.close()
        return 'Project not found', 404
    ensure_project_site_details(conn, project_id)
    details = conn.execute(
        """SELECT * FROM site_details
           WHERE project_id = ? AND name = 'Sale Deed'""",
        (project_id,)
    ).fetchall()
    conn.commit()
    conn.close()
    selected_id = request.args.get('selected', type=int)
    is_complete = request.args.get('complete') == '1'
    selected_index = next(
        (index for index, detail in enumerate(details) if detail['id'] == selected_id),
        0
    )
    selected_detail = None if is_complete else (
        details[selected_index] if details else None
    )
    incomplete_details = [
        detail for detail in details if not detail['filename']
    ]
    return render_template(
        'site_details.html', project=project, details=details,
        selected_detail=selected_detail, selected_index=selected_index,
        is_complete=is_complete, incomplete_details=incomplete_details
    )

@app.route('/site-details/<int:detail_id>/upload', methods=['POST'])
def upload_site_detail_document(detail_id):
    conn = get_db()
    detail = get_site_detail(conn, detail_id)
    if not detail:
        conn.close()
        return 'Site detail not found', 404
    file = request.files.get('document')
    if not file or not file.filename:
        flash('Choose a PDF before uploading.', 'error')
        conn.close()
        return redirect(url_for(
            'project_site_details', project_id=detail['project_id'], selected=detail_id
        ))
    try:
        stored_name = save_uploaded_document(file, detail['project_id'], detail_id)
    except ValueError as error:
        flash(str(error), 'error')
        conn.close()
        return redirect(url_for(
            'project_site_details', project_id=detail['project_id'], selected=detail_id
        ))

    old_filename = detail['filename']
    conn.execute(
        "UPDATE site_details SET filename = ?, status = 'Pending' WHERE id = ?",
        (stored_name, detail_id)
    )
    conn.commit()
    conn.close()
    remove_uploaded_document(old_filename)
    flash('PDF uploaded successfully. Click Next when you are ready to continue.', 'success')
    return redirect(url_for(
        'project_site_details', project_id=detail['project_id'], selected=detail_id
    ))

@app.route('/site-details/<int:detail_id>/document')
def view_site_detail_document(detail_id):
    conn = get_db()
    detail = get_site_detail(conn, detail_id)
    conn.close()
    if not detail:
        return 'Site detail not found', 404
    if not detail['filename']:
        return 'No PDF has been uploaded for this site detail.', 404
    return send_from_directory(
        app.config['UPLOAD_FOLDER'], detail['filename'], as_attachment=False
    )

@app.route('/site-details/<int:detail_id>/download')
def download_site_detail_document(detail_id):
    conn = get_db()
    detail = get_site_detail(conn, detail_id)
    conn.close()
    if not detail:
        return 'Site detail not found', 404
    if not detail['filename']:
        return 'No PDF has been uploaded for this site detail.', 404
    return send_from_directory(
        app.config['UPLOAD_FOLDER'], detail['filename'], as_attachment=True
    )

@app.route('/site-details/<int:detail_id>/edit', methods=['GET', 'POST'])
def edit_site_detail(detail_id):
    conn = get_db()
    detail = get_site_detail(conn, detail_id)
    if not detail:
        conn.close()
        return 'Site detail not found', 404
    if request.method == 'POST':
        value = request.form.get('value', '').strip()
        status = request.form.get('status', 'Pending')
        if len(value) > 1000:
            flash('Site detail values must be 1,000 characters or fewer.', 'error')
        elif status not in {'Pending', 'Verified', 'N/A'}:
            flash('Choose a valid document review status.', 'error')
        elif status == 'Verified' and not (request.files.get('document') and request.files['document'].filename or detail['filename']):
            flash('Upload a supporting PDF before marking this site detail verified.', 'error')
        else:
            file = request.files.get('document')
            new_filename = None
            if file and file.filename:
                try:
                    new_filename = save_uploaded_document(
                        file, detail['project_id'], detail_id
                    )
                except ValueError as error:
                    flash(str(error), 'error')
                    conn.close()
                    return redirect(url_for('edit_site_detail', detail_id=detail_id))
            value_changed = value != detail['value']
            if (new_filename or value_changed) and status == 'Verified':
                status = 'Pending'
            old_filename = detail['filename'] if new_filename else None
            conn.execute(
                'UPDATE site_details SET value = ?, status = ?, filename = ? WHERE id = ?',
                (value, status, new_filename or detail['filename'], detail_id)
            )
            conn.commit()
            conn.close()
            remove_uploaded_document(old_filename)
            flash('Site detail updated.', 'success')
            return redirect(url_for(
                'project_site_details', project_id=detail['project_id'],
                selected=detail_id
            ))
    conn.close()
    return render_template('site_detail_edit.html', detail=detail)

@app.route('/site-details/<int:detail_id>/delete', methods=['POST'])
def delete_site_detail_document(detail_id):
    conn = get_db()
    detail = get_site_detail(conn, detail_id)
    if not detail:
        conn.close()
        return 'Site detail not found', 404
    conn.execute(
        "UPDATE site_details SET filename = NULL, status = 'Pending' WHERE id = ?",
        (detail_id,)
    )
    conn.commit()
    conn.close()
    remove_uploaded_document(detail['filename'])
    flash('Uploaded PDF deleted. The saved site detail was kept.', 'success')
    return redirect(url_for(
        'project_site_details', project_id=detail['project_id'], selected=detail_id
    ))

@app.route('/projects/<int:project_id>/approvals')
def project_approvals(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    if not project:
        conn.close()
        return 'Project not found', 404
    ensure_project_approvals(conn, project_id)
    approval_names = [name for name, _ in APPROVAL_DEFAULTS]
    approvals = conn.execute(
        """SELECT * FROM approvals
           WHERE project_id = ? AND name IN ({})
           ORDER BY CASE name
               WHEN 'Mother Deed' THEN 0
               WHEN 'Encumbrance Certificate (EC)' THEN 1
               WHEN 'A & B Khata Certificate' THEN 2
               WHEN 'Property Tax Receipts' THEN 3
               ELSE 4
           END""".format(', '.join('?' for _ in approval_names)),
        (project_id, *approval_names)
    ).fetchall()
    conn.commit()
    can_apply = project_requires_approval(conn, project_id)
    conn.close()
    selected_id = request.args.get('selected', type=int)
    is_complete = request.args.get('complete') == '1'
    selected_index = next(
        (index for index, approval in enumerate(approvals) if approval['id'] == selected_id),
        0
    )
    selected_approval = None if is_complete else (
        approvals[selected_index] if approvals else None
    )
    incomplete_approvals = [
        approval for approval in approvals
        if approval['status'] != 'N/A' and not approval['filename']
    ]
    return render_template(
        'approvals.html', project=project, approvals=approvals,
        selected_approval=selected_approval, selected_index=selected_index,
        is_complete=is_complete, incomplete_approvals=incomplete_approvals,
        can_apply=can_apply
    )

@app.route('/projects/<int:project_id>/property-documents')
def project_property_documents(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    if not project:
        conn.close()
        return 'Project not found', 404

    ensure_project_site_details(conn, project_id)
    ensure_project_approvals(conn, project_id)
    site_deed = conn.execute(
        "SELECT * FROM site_details WHERE project_id = ? AND name = 'Sale Deed'",
        (project_id,)
    ).fetchone()
    approvals = {
        row['name']: row for row in conn.execute(
            'SELECT * FROM approvals WHERE project_id = ?', (project_id,)
        ).fetchall()
    }
    conn.commit()
    conn.close()

    steps = []
    for key, kind, name in PROPERTY_DOCUMENT_STEPS:
        document = site_deed if kind == 'site detail' else approvals.get(name)
        if document:
            steps.append({
                'key': key,
                'kind': kind,
                'name': name,
                'document': document,
            })

    selected_key = request.args.get('step')
    selected_index = next(
        (index for index, step in enumerate(steps) if step['key'] == selected_key),
        0
    )
    is_complete = request.args.get('complete') == '1'
    incomplete_steps = [
        step for step in steps if not step['document']['filename']
    ]
    return render_template(
        'property_documents.html', project=project, steps=steps,
        selected_step=None if is_complete else steps[selected_index] if steps else None,
        selected_index=selected_index, is_complete=is_complete,
        incomplete_steps=incomplete_steps
    )

@app.route(
    '/projects/<int:project_id>/property-documents/<step_key>/upload',
    methods=['POST']
)
def upload_property_document(project_id, step_key):
    step = next(
        (item for item in PROPERTY_DOCUMENT_STEPS if item[0] == step_key),
        None
    )
    if not step:
        return 'Property document step not found', 404

    conn = get_db()
    project = conn.execute(
        'SELECT id FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    if not project:
        conn.close()
        return 'Project not found', 404

    key, kind, name = step
    if kind == 'site detail':
        ensure_project_site_details(conn, project_id)
        document = conn.execute(
            "SELECT * FROM site_details WHERE project_id = ? AND name = ?",
            (project_id, name)
        ).fetchone()
    else:
        ensure_project_approvals(conn, project_id)
        document = conn.execute(
            'SELECT * FROM approvals WHERE project_id = ? AND name = ?',
            (project_id, name)
        ).fetchone()

    file = request.files.get('document')
    if not file or not file.filename:
        conn.close()
        flash('Choose a PDF before uploading.', 'error')
        return redirect(url_for(
            'project_property_documents', project_id=project_id, step=key
        ))

    try:
        stored_name = save_uploaded_document(file, project_id, document['id'])
    except ValueError as error:
        conn.close()
        flash(str(error), 'error')
        return redirect(url_for(
            'project_property_documents', project_id=project_id, step=key
        ))

    old_filename = document['filename']
    if kind == 'site detail':
        conn.execute(
            "UPDATE site_details SET filename = ?, status = 'Pending' WHERE id = ?",
            (stored_name, document['id'])
        )
    else:
        conn.execute(
            "UPDATE approvals SET filename = ?, status = 'Pending' WHERE id = ?",
            (stored_name, document['id'])
        )
    conn.commit()
    conn.close()
    remove_uploaded_document(old_filename)

    step_index = next(
        index for index, item in enumerate(PROPERTY_DOCUMENT_STEPS)
        if item[0] == key
    )
    next_index = step_index + 1
    flash(f'{name} PDF uploaded successfully.', 'success')
    if next_index < len(PROPERTY_DOCUMENT_STEPS):
        return redirect(url_for(
            'project_property_documents', project_id=project_id,
            step=PROPERTY_DOCUMENT_STEPS[next_index][0]
        ))
    return redirect(url_for(
        'project_property_documents', project_id=project_id, complete=1
    ))

@app.route('/projects/<int:project_id>/plans')
def project_plans(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    can_apply = bool(project and project_requires_approval(conn, project_id)) if project else False
    conn.close()
    if not project:
        return 'Project not found', 404
    return render_template('plans.html', project=project, can_apply=can_apply)

@app.route('/projects/<int:project_id>/construction-budget', methods=['GET', 'POST'])
def project_construction_budget(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    if not project:
        conn.close()
        return 'Project not found', 404

    ensure_construction_plan(conn, project_id)
    if request.method == 'POST':
        items = conn.execute(
            '''SELECT id FROM construction_budget_items
               WHERE project_id = ? ORDER BY id''',
            (project_id,)
        ).fetchall()
        amounts = {}
        try:
            for item in items:
                raw_amount = request.form.get(f"amount_{item['id']}", '').strip()
                amount = float(raw_amount)
                if not math.isfinite(amount) or not 0 <= amount <= 1_000_000_000_000:
                    raise ValueError
                amounts[item['id']] = amount
        except ValueError:
            flash('Enter a valid non-negative amount for every budget item.', 'error')
        else:
            raw_duration = request.form.get('duration_months', '').strip()
            start_date = request.form.get('start_date', '').strip()
            notes = request.form.get('notes', '').strip()
            try:
                duration_months = int(raw_duration)
                if not 1 <= duration_months <= 120:
                    raise ValueError
            except ValueError:
                flash('Enter a construction duration from 1 to 120 months.', 'error')
            else:
                try:
                    if start_date:
                        date.fromisoformat(start_date)
                except ValueError:
                    flash('Enter a valid planned start date.', 'error')
                else:
                    if len(notes) > 2000:
                        flash('Schedule notes must be 2,000 characters or fewer.', 'error')
                    else:
                        conn.executemany(
                            'UPDATE construction_budget_items SET amount = ? WHERE id = ? AND project_id = ?',
                            [
                                (amount, item_id, project_id)
                                for item_id, amount in amounts.items()
                            ]
                        )
                        conn.execute(
                            '''UPDATE construction_schedules
                               SET duration_months = ?, start_date = ?, notes = ?
                               WHERE project_id = ?''',
                            (duration_months, start_date, notes, project_id)
                        )
                        conn.commit()
                        conn.close()
                        flash('Construction budget and schedule saved.', 'success')
                        return redirect(url_for(
                            'project_construction_budget', project_id=project_id
                        ))

    budget_items = conn.execute(
        '''SELECT id, section, category, amount
           FROM construction_budget_items
           WHERE project_id = ? ORDER BY id''',
        (project_id,)
    ).fetchall()
    schedule = conn.execute(
        'SELECT * FROM construction_schedules WHERE project_id = ?',
        (project_id,)
    ).fetchone()
    customer_payments = conn.execute(
        '''SELECT id, customer_name, completed_work, amount_received, payment_date
           FROM customer_payments WHERE project_id = ?
           ORDER BY payment_date DESC, id DESC''',
        (project_id,)
    ).fetchall()
    conn.commit()
    conn.close()

    raw_values = request.form if request.method == 'POST' else {}
    floor_budgets = []
    for floor in CONSTRUCTION_FLOORS:
        floor_items = [
            {
                'id': item['id'],
                'category': item['category'],
                'amount': raw_values.get(
                    f"amount_{item['id']}", item['amount']
                ),
            }
            for item in budget_items if item['section'] == floor
        ]
        floor_budgets.append({
            'name': floor,
            'items': floor_items,
            'total': sum(
                item['amount'] for item in budget_items
                if item['section'] == floor
            ),
        })
    common_items = [
        {
            'id': item['id'],
            'category': item['category'],
            'amount': raw_values.get(
                f"amount_{item['id']}", item['amount']
            ),
        }
        for item in budget_items if item['section'] == 'Common'
    ]
    common_total = sum(
        item['amount'] for item in budget_items if item['section'] == 'Common'
    )
    budget_total = sum(floor['total'] for floor in floor_budgets) + common_total
    payments_total = sum(
        payment['amount_received'] for payment in customer_payments
    )
    return render_template(
        'construction_budget.html',
        project=project,
        floors=floor_budgets,
        common_items=common_items,
        common_total=common_total,
        budget_total=budget_total,
        schedule=schedule,
        customer_payments=customer_payments,
        payments_total=payments_total,
    )

@app.route('/projects/<int:project_id>/customer-payments', methods=['POST'])
def save_customer_payment(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT id FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    if not project:
        conn.close()
        return 'Project not found', 404

    action = request.form.get('action', '')
    if action == 'delete':
        try:
            payment_id = int(request.form.get('payment_id', ''))
        except ValueError:
            conn.close()
            return 'Payment not found', 404
        cursor = conn.execute(
            'DELETE FROM customer_payments WHERE id = ? AND project_id = ?',
            (payment_id, project_id)
        )
        conn.commit()
        conn.close()
        if cursor.rowcount != 1:
            return 'Payment not found', 404
        flash('Customer payment entry deleted.', 'success')
        return redirect(url_for(
            'project_construction_budget', project_id=project_id
        ))

    customer_name = request.form.get('customer_name', '').strip()
    completed_work = request.form.get('completed_work', '').strip()
    raw_amount = request.form.get('amount_received', '').strip()
    payment_date = request.form.get('payment_date', '').strip()
    if not customer_name or len(customer_name) > 100:
        flash('Enter a customer name (100 characters or fewer).', 'error')
    elif not completed_work or len(completed_work) > 500:
        flash('Enter the completed work (500 characters or fewer).', 'error')
    else:
        try:
            amount_received = float(raw_amount)
            if not math.isfinite(amount_received) or not 0 < amount_received <= 1_000_000_000_000:
                raise ValueError
        except ValueError:
            flash('Enter a payment amount greater than ₹0.', 'error')
        else:
            try:
                if payment_date:
                    date.fromisoformat(payment_date)
            except ValueError:
                flash('Enter a valid payment date.', 'error')
            else:
                if action == 'add':
                    conn.execute(
                        '''INSERT INTO customer_payments
                           (project_id, customer_name, completed_work,
                            amount_received, payment_date)
                           VALUES (?, ?, ?, ?, ?)''',
                        (
                            project_id, customer_name, completed_work,
                            amount_received, payment_date
                        )
                    )
                    message = 'Customer payment entry added.'
                elif action == 'edit':
                    try:
                        payment_id = int(request.form.get('payment_id', ''))
                    except ValueError:
                        conn.close()
                        return 'Payment not found', 404
                    cursor = conn.execute(
                        '''UPDATE customer_payments
                           SET customer_name = ?, completed_work = ?,
                               amount_received = ?, payment_date = ?
                           WHERE id = ? AND project_id = ?''',
                        (
                            customer_name, completed_work, amount_received,
                            payment_date, payment_id, project_id
                        )
                    )
                    if cursor.rowcount != 1:
                        conn.close()
                        return 'Payment not found', 404
                    message = 'Customer payment entry updated.'
                else:
                    conn.close()
                    abort(400, 'Choose a valid customer payment action.')
                conn.commit()
                conn.close()
                flash(message, 'success')
                return redirect(url_for(
                    'project_construction_budget', project_id=project_id
                ))

    conn.close()
    return redirect(url_for(
        'project_construction_budget', project_id=project_id
    ))

@app.route('/projects/<int:project_id>/apply', methods=['POST'])
def apply_for_approval(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    if not project:
        conn.close()
        return 'Project not found', 404
    if not project_requires_approval(conn, project_id):
        conn.close()
        flash(
            'Complete all site details, upload the project plan, and submit all required approval documents before applying.',
            'error'
        )
        return redirect(url_for('project_plans', project_id=project_id))

    conn.execute(
        'UPDATE projects SET approval_status = ? WHERE id = ?',
        ('Applied', project_id)
    )
    conn.commit()
    conn.close()
    flash('Approval application submitted successfully.', 'success')
    return redirect(url_for('project_plans', project_id=project_id))

@app.route('/projects/<int:project_id>/plans/upload', methods=['POST'])
def upload_project_plan(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    if not project:
        conn.close()
        return 'Project not found', 404
    file = request.files.get('document')
    if not file or not file.filename:
        flash('Choose a PDF before uploading.', 'error')
        conn.close()
        return redirect(url_for('project_plans', project_id=project_id))
    try:
        stored_name = save_uploaded_document(file, project_id, 'blueprint')
    except ValueError as error:
        flash(str(error), 'error')
        conn.close()
        return redirect(url_for('project_plans', project_id=project_id))

    old_filename = project['filename']
    conn.execute(
        'UPDATE projects SET filename = ? WHERE id = ?', (stored_name, project_id)
    )
    conn.commit()
    conn.close()
    remove_uploaded_document(old_filename)
    flash('Plans PDF uploaded successfully.', 'success')
    return redirect(url_for('project_plans', project_id=project_id))

@app.route('/projects/<int:project_id>/plans/document')
def view_project_plan(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    conn.close()
    if not project:
        return 'Project not found', 404
    if not project['filename']:
        return 'No plans PDF has been uploaded for this project.', 404
    return send_from_directory(
        app.config['UPLOAD_FOLDER'], project['filename'], as_attachment=False
    )

@app.route('/projects/<int:project_id>/plans/download')
def download_project_plan(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    conn.close()
    if not project:
        return 'Project not found', 404
    if not project['filename']:
        return 'No plans PDF has been uploaded for this project.', 404
    return send_from_directory(
        app.config['UPLOAD_FOLDER'], project['filename'], as_attachment=True
    )

@app.route('/projects/<int:project_id>/plans/delete', methods=['POST'])
def delete_project_plan(project_id):
    conn = get_db()
    project = conn.execute(
        'SELECT * FROM projects WHERE id = ?', (project_id,)
    ).fetchone()
    if not project:
        conn.close()
        return 'Project not found', 404
    old_filename = project['filename']
    conn.execute('UPDATE projects SET filename = NULL WHERE id = ?', (project_id,))
    conn.commit()
    conn.close()
    remove_uploaded_document(old_filename)
    flash('Plans PDF deleted.', 'success')
    return redirect(url_for('project_plans', project_id=project_id))

@app.route('/approvals/<int:approval_id>/upload', methods=['POST'])
def upload_approval(approval_id):
    conn = get_db()
    approval = get_approval(conn, approval_id)
    if not approval:
        conn.close()
        return 'Approval not found', 404
    file = request.files.get('document')
    if not file or not file.filename:
        flash('Choose a document before uploading.', 'error')
        conn.close()
        return redirect(url_for(
            'project_approvals', project_id=approval['project_id'], selected=approval_id
        ))
    try:
        stored_name = save_uploaded_document(file, approval['project_id'], approval_id)
    except ValueError as error:
        flash(str(error), 'error')
        conn.close()
        return redirect(url_for(
            'project_approvals', project_id=approval['project_id'], selected=approval_id
        ))

    old_filename = approval['filename']
    conn.execute(
        'UPDATE approvals SET filename = ?, status = ? WHERE id = ?',
        (stored_name, 'Pending', approval_id)
    )
    conn.commit()
    conn.close()
    remove_uploaded_document(old_filename)
    flash('PDF uploaded successfully. Click Next when you are ready to continue.', 'success')
    return redirect(url_for(
        'project_approvals', project_id=approval['project_id'], selected=approval_id
    ))

@app.route('/approvals/<int:approval_id>/document')
def view_approval_document(approval_id):
    conn = get_db()
    approval = get_approval(conn, approval_id)
    conn.close()
    if not approval:
        return 'Approval not found', 404
    if not approval['filename']:
        return 'No document has been uploaded for this approval.', 404
    return send_from_directory(
        app.config['UPLOAD_FOLDER'], approval['filename'], as_attachment=False
    )

@app.route('/approvals/<int:approval_id>/download')
def download_approval_document(approval_id):
    conn = get_db()
    approval = get_approval(conn, approval_id)
    conn.close()
    if not approval:
        return 'Approval not found', 404
    if not approval['filename']:
        return 'No document has been uploaded for this approval.', 404
    return send_from_directory(
        app.config['UPLOAD_FOLDER'], approval['filename'], as_attachment=True
    )

@app.route('/approvals/<int:approval_id>/edit', methods=['GET', 'POST'])
def edit_approval(approval_id):
    conn = get_db()
    approval = get_approval(conn, approval_id)
    if not approval:
        conn.close()
        return 'Approval not found', 404
    if request.method == 'POST':
        notes = request.form.get('notes', '').strip()
        requested_status = request.form.get('status', 'Pending')
        if len(notes) > 1000:
            flash('Notes must be 1,000 characters or fewer.', 'error')
        elif requested_status not in {'Pending', 'Verified', 'N/A'}:
            flash('Choose a valid document review status.', 'error')
        elif requested_status == 'Verified' and not approval['filename'] and not (
            request.files.get('document') and request.files['document'].filename
        ):
            flash('Upload a supporting PDF before marking this document verified.', 'error')
        else:
            return save_approval_edits(conn, approval, notes, requested_status)
    conn.close()
    return render_template('approval_edit.html', approval=approval)

def save_approval_edits(conn, approval, notes, requested_status):
    approval_id = approval['id']
    file = request.files.get('document')
    new_filename = None
    if file and file.filename:
        try:
            new_filename = save_uploaded_document(
                file, approval['project_id'], approval_id
            )
        except ValueError as error:
            flash(str(error), 'error')
            conn.close()
            return redirect(url_for('edit_approval', approval_id=approval_id))
    status = 'Pending' if new_filename else requested_status
    old_filename = approval['filename'] if new_filename else None
    conn.execute(
        'UPDATE approvals SET status = ?, notes = ?, filename = ? WHERE id = ?',
        (status, notes, new_filename or approval['filename'], approval_id)
    )
    conn.commit()
    conn.close()
    remove_uploaded_document(old_filename)
    flash('Document details updated. Review status is for internal tracking and does not confirm authenticity with an issuing authority.', 'success')
    return redirect(url_for(
        'project_approvals', project_id=approval['project_id'],
        selected=approval_id
    ))

@app.route('/approvals/<int:approval_id>/delete', methods=['POST'])
def delete_approval_document(approval_id):
    conn = get_db()
    approval = get_approval(conn, approval_id)
    if not approval:
        conn.close()
        return 'Approval not found', 404
    reset_status = 'N/A' if approval['name'] == 'Land Conversion' else 'Pending'
    conn.execute(
        'UPDATE approvals SET filename = NULL, status = ? WHERE id = ?',
        (reset_status, approval_id)
    )
    conn.commit()
    conn.close()
    remove_uploaded_document(approval['filename'])
    flash('Uploaded document deleted.', 'success')
    return redirect(url_for(
        'project_approvals', project_id=approval['project_id'], selected=approval_id
    ))

@app.route('/report/<int:id>')
def generate_pdf(id):
    conn = get_db()
    project = conn.execute('SELECT * FROM projects WHERE id = ?', (id,)).fetchone()
    conn.close()
    
    if not project:
        return "Project not found", 404

    pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], f"project_{id}_report.pdf")
    c = canvas.Canvas(pdf_path, pagesize=letter)
    
    # PDF Layout
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, 750, "SIDDA CONSTRUCTION - Project Report")
    c.setFont("Helvetica", 12)
    c.drawString(50, 710, f"Project Name: {project['project_name']}")
    c.drawString(50, 680, f"Client Name: {project['client_name']}")
    c.drawString(50, 650, f"Budget (INR): {format_indian_currency(project['budget'])}")
    c.drawString(50, 620, f"Status: {project['status']}")
    c.save()

    return send_file(pdf_path, as_attachment=True)

if __name__ == '__main__':
    init_db()
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1')