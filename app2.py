# app.py — ACP Gestión (Flask 3+) — dotenv + Libro único + comprobante texto en Movimientos

from datetime import datetime, date
from calendar import monthrange
from functools import wraps
from io import StringIO
import os, csv

from dotenv import load_dotenv
load_dotenv()  # carga variables de .env si existe

from flask import (
    Flask, request, redirect, url_for, session, flash, Response,
    send_file, render_template_string as render
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import text

# --------------------
# Configuración
# --------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'changeme')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('SQLALCHEMY_DATABASE_URI', 'sqlite:///asociacion.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Archivos de cuotas (comprobantes adjuntos)
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'comprobantes')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTS = {'pdf', 'jpg', 'jpeg', 'png'}
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Categorías fijas
INGRESO_CATS = ['Cuotas socios','Escuela','Eventos','Donaciones','Venta de comidas','Merchandising','Otros']
SALIDA_CATS  = ['Alquileres','Préstamos','Servicios','Viáticos','Utilería','Compra mercadería','Costo mercadería vendida','Otros']

# Email (SMTP) - opcional
SMTP_ENABLED = os.getenv('SMTP_ENABLED', 'true').lower() == 'true'
SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASS = os.getenv('SMTP_PASS', '')
SMTP_FROM = os.getenv('SMTP_FROM', SMTP_USER or 'no-reply@example.com')

# WhatsApp (Twilio) - opcional
WHATSAPP_ENABLED = os.getenv('WHATSAPP_ENABLED', 'false').lower() == 'true'
TWILIO_SID = os.getenv('TWILIO_SID', '')
TWILIO_TOKEN = os.getenv('TWILIO_TOKEN', '')
TWILIO_WA_FROM = os.getenv('TWILIO_WA_FROM', 'whatsapp:+14155238886')

db = SQLAlchemy(app)

def allowed_file(fn: str) -> bool:
    return '.' in fn and fn.rsplit('.', 1)[1].lower() in ALLOWED_EXTS

def ultimo_dia_mes(y, m):
    return monthrange(y, m)[1]

# --------------------
# Modelos
# --------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='admin')  # admin | operador | consulta
    def set_password(self, pw): self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)

class Socio(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120))
    dni = db.Column(db.String(20))
    telefono = db.Column(db.String(30))
    activo = db.Column(db.Boolean, default=True)
    fecha_alta = db.Column(db.Date, default=date.today)
    cuota_mensual = db.Column(db.Float, default=0.0)

class Movimiento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(10), nullable=False)              # 'ingreso' | 'salida'
    categoria = db.Column(db.String(60))
    origen = db.Column(db.String(20), default='manual')          # 'manual' | 'cuota' | 'merch' | 'evento' | 'escuela'
    socio_id = db.Column(db.Integer, db.ForeignKey('socio.id'))  # opcional (trazabilidad)
    cuota_id = db.Column(db.Integer, db.ForeignKey('cuota.id'))  # opcional (trazabilidad)
    stockmov_id = db.Column(db.Integer)                          # opcional (cuando agregues merch)
    monto = db.Column(db.Float, nullable=False, default=0.0)
    fecha = db.Column(db.Date, default=date.today)
    descripcion = db.Column(db.String(255))
    # comprobante como texto (solo Movimientos)
    comp_tipo = db.Column(db.String(30))   # Recibo, Factura, Ticket, etc.
    comp_nro  = db.Column(db.String(40))   # A-0001-00001234
    comprobantes = db.relationship('Comprobante', backref='mov', lazy=True)  # (no usado en /movimientos)

class Evento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(120), nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    lugar = db.Column(db.String(120))
    descripcion = db.Column(db.String(255))

class Inscripcion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    evento_id = db.Column(db.Integer, db.ForeignKey('evento.id'), nullable=False)
    socio_id  = db.Column(db.Integer, db.ForeignKey('socio.id'), nullable=False)
    fecha = db.Column(db.Date, default=date.today)
    __table_args__ = (db.UniqueConstraint('evento_id','socio_id', name='uq_evento_socio'),)

class Cuota(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    socio_id = db.Column(db.Integer, db.ForeignKey('socio.id'), nullable=False)
    periodo = db.Column(db.String(7), nullable=False)  # 'YYYY-MM'
    monto = db.Column(db.Float, nullable=False, default=0.0)
    fecha_venc = db.Column(db.Date, nullable=False)
    pagada = db.Column(db.Boolean, default=False)
    fecha_pago = db.Column(db.Date)
    nota = db.Column(db.String(255))
    comprobantes = db.relationship('Comprobante', backref='cuota', lazy=True)
    __table_args__ = (db.UniqueConstraint('socio_id','periodo', name='uq_cuota_socio_periodo'),)

class Comprobante(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    mov_id = db.Column(db.Integer, db.ForeignKey('movimiento.id'))
    cuota_id = db.Column(db.Integer, db.ForeignKey('cuota.id'))

# --------------------
# Inicialización + mini-migraciones
# --------------------
def init_db():
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            u = User(username='admin', role='admin')
            u.set_password('admin123')
            db.session.add(u)
            db.session.commit()

def migrate_sqlite():
    """Agrega socio.cuota_mensual si falta y asegura tablas."""
    with app.app_context():
        cols = [r[1] for r in db.session.execute(text("PRAGMA table_info(socio)")).fetchall()]
        if 'cuota_mensual' not in cols:
            db.session.execute(text("ALTER TABLE socio ADD COLUMN cuota_mensual FLOAT DEFAULT 0.0"))
        db.create_all()
        db.session.commit()

def migrate_movimientos_unificado():
    """Agrega columnas de trazabilidad en movimiento si faltan."""
    with app.app_context():
        cols = [r[1] for r in db.session.execute(text("PRAGMA table_info(movimiento)")).fetchall()]
        stmts = []
        if 'origen'      not in cols: stmts.append("ALTER TABLE movimiento ADD COLUMN origen VARCHAR(20) DEFAULT 'manual'")
        if 'socio_id'    not in cols: stmts.append("ALTER TABLE movimiento ADD COLUMN socio_id INTEGER")
        if 'cuota_id'    not in cols: stmts.append("ALTER TABLE movimiento ADD COLUMN cuota_id INTEGER")
        if 'stockmov_id' not in cols: stmts.append("ALTER TABLE movimiento ADD COLUMN stockmov_id INTEGER")
        for s in stmts: db.session.execute(text(s))
        db.session.commit()

def migrate_comprobantes_texto_mov():
    """Agrega comp_tipo y comp_nro en movimiento si faltan (solo para Ingresos/Salidas)."""
    with app.app_context():
        cols = [r[1] for r in db.session.execute(text("PRAGMA table_info(movimiento)")).fetchall()]
        if 'comp_tipo' not in cols:
            db.session.execute(text("ALTER TABLE movimiento ADD COLUMN comp_tipo VARCHAR(30)"))
        if 'comp_nro' not in cols:
            db.session.execute(text("ALTER TABLE movimiento ADD COLUMN comp_nro VARCHAR(40)"))
        db.session.commit()

init_db()
migrate_sqlite()
migrate_movimientos_unificado()
migrate_comprobantes_texto_mov()

# --------------------
# Helpers
# --------------------
def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if 'uid' not in session:
            return redirect(url_for('login', next=request.path))
        return f(*a, **k)
    return w

def role_required(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if session.get('role') not in roles:
                flash('No autorizado')
                return redirect(url_for('dashboard'))
            return f(*a, **k)
        return w
    return deco

def send_email(to, subject, body):
    if not (SMTP_ENABLED and SMTP_USER and SMTP_PASS):
        return False, 'SMTP no configurado'
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = SMTP_FROM
    msg['To'] = to
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, [to], msg.as_string())
        return True, 'OK'
    except Exception as e:
        return False, str(e)

def send_whatsapp(to_e164, body):
    if not WHATSAPP_ENABLED:
        return False, 'WhatsApp no habilitado'
    try:
        from twilio.rest import Client
        cli = Client(TWILIO_SID, TWILIO_TOKEN)
        cli.messages.create(from_=TWILIO_WA_FROM, to=f'whatsapp:{to_e164}', body=body)
        return True, 'OK'
    except Exception as e:
        return False, str(e)

# --------------------
# Layout (logo grande + oliva)
# --------------------
LAYOUT = """
<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title or 'ACP' }}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
      :root { --oliva:#6B8E23; --rojo:#C8102E; --negro:#111; }
      .navbar-acp { background-color: var(--oliva); }
      .badge-rojo { background-color: var(--rojo); }
      .text-oliva { color: var(--oliva); }
      .btn-oliva { background: var(--oliva); color:#fff; border-color: var(--oliva); }
      .btn-oliva:hover { filter: brightness(0.95); color:#fff; }
      a { text-decoration: none; }
      .brand-logo { max-height: 64px; }
      @media (max-width: 576px){ .brand-logo{ max-height: 48px; } }
    </style>
  </head>
  <body class="bg-light">
    <nav class="navbar navbar-expand-lg navbar-dark navbar-acp">
      <div class="container-fluid">
        <a class="navbar-brand d-flex align-items-center" href="{{ url_for('dashboard') }}">
          <img src="{{ url_for('static', filename='logo_acp.png') }}" alt="ACP" class="me-2 brand-logo">
          <span>Asociación Cultural Palestina</span>
        </a>
        <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#nav"><span class="navbar-toggler-icon"></span></button>
        <div id="nav" class="collapse navbar-collapse">
          <ul class="navbar-nav me-auto">
            {% if session.get('uid') %}
              <li class="nav-item"><a class="nav-link" href="{{ url_for('socios') }}">Socios</a></li>
              <li class="nav-item"><a class="nav-link" href="{{ url_for('movimientos') }}">Ingresos/Salidas</a></li>
              <li class="nav-item"><a class="nav-link" href="{{ url_for('eventos') }}">Eventos</a></li>
              <li class="nav-item"><a class="nav-link" href="{{ url_for('cuotas') }}">Cuotas</a></li>
              <li class="nav-item"><a class="nav-link" href="{{ url_for('morosidad') }}">Morosidad</a></li>
              {% if session.get('role') == 'admin' %}
                <li class="nav-item"><a class="nav-link" href="{{ url_for('usuarios') }}">Usuarios</a></li>
              {% endif %}
            {% endif %}
          </ul>
          <ul class="navbar-nav">
            {% if session.get('uid') %}
              <li class="nav-item"><span class="navbar-text me-3">Rol: {{ session.get('role') }}</span></li>
              <li class="nav-item"><a class="nav-link" href="{{ url_for('logout') }}">Salir</a></li>
            {% else %}
              <li class="nav-item"><a class="nav-link" href="{{ url_for('login') }}">Ingresar</a></li>
            {% endif %}
          </ul>
        </div>
      </div>
    </nav>
    <main class="container py-4">
      {% with msgs = get_flashed_messages() %}
        {% if msgs %}<div class="alert alert-info">{{ msgs[0] }}</div>{% endif %}
      {% endwith %}
      {{ body|safe }}
    </main>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
  </body>
</html>
"""
def page(body, **ctx): return render(LAYOUT, body=body, **ctx)

# --------------------
# Auth
# --------------------
@app.get('/login')
def login():
    body = """
    <div class="row justify-content-center">
      <div class="col-md-4">
        <h3 class="text-oliva">Ingresar</h3>
        <form method="post">
          <div class="mb-3"><label class="form-label">Usuario</label><input name="username" class="form-control" required></div>
          <div class="mb-3"><label class="form-label">Contraseña</label><input type="password" name="password" class="form-control" required></div>
          <button class="btn btn-oliva w-100">Entrar</button>
        </form>
        <div class="form-text mt-2">Inicial: admin / admin123</div>
      </div>
    </div>"""
    return page(body, title='Login')

@app.post('/login')
def login_post():
    u = User.query.filter_by(username=request.form.get('username','').strip()).first()
    if u and u.check_password(request.form.get('password','')):
        session['uid'] = u.id
        session['role'] = u.role
        return redirect(request.args.get('next') or url_for('dashboard'))
    flash('Credenciales inválidas')
    return redirect(url_for('login'))

@app.get('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --------------------
# Dashboard
# --------------------
@app.get('/')
@login_required
def dashboard():
    total_socios = Socio.query.count()
    activos = Socio.query.filter_by(activo=True).count()
    ingresos = db.session.query(db.func.sum(db.case((Movimiento.tipo=='ingreso', Movimiento.monto), else_=0.0))).scalar() or 0.0
    salidas  = db.session.query(db.func.sum(db.case((Movimiento.tipo=='salida',  Movimiento.monto), else_=0.0))).scalar() or 0.0
    saldo = ingresos - salidas
    prox = Evento.query.order_by(Evento.fecha.asc()).limit(5).all()
    impagas = Cuota.query.filter_by(pagada=False).count()

    body = render("""
    <div class="row g-3">
      <div class="col-md-3"><div class="card"><div class="card-body"><small class="text-muted">Socios</small><div class="fs-3">{{ total_socios }}</div></div></div></div>
      <div class="col-md-3"><div class="card"><div class="card-body"><small class="text-muted">Activos</small><div class="fs-3">{{ activos }}</div></div></div></div>
      <div class="col-md-3"><div class="card"><div class="card-body"><small class="text-muted">Saldo</small><div class="fs-3">$ {{ '%.2f' % saldo }}</div></div></div></div>
      <div class="col-md-3"><div class="card"><div class="card-body"><small class="text-muted">Cuotas impagas</small><div class="fs-3">{{ impagas }}</div></div></div></div>
    </div>

    <div class="card mt-4">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span>Próximos eventos</span>
        <a class="btn btn-sm btn-outline-dark" href="{{ url_for('eventos') }}">Ver todos</a>
      </div>
      <div class="card-body">
        {% if prox %}
          <ul class="mb-0">{% for e in prox %}<li>{{ e.titulo }} — {{ e.fecha.strftime('%d/%m/%Y') }} @ {{ e.lugar or '—' }}</li>{% endfor %}</ul>
        {% else %}<em>No hay eventos cargados.</em>{% endif %}
      </div>
    </div>
    """, total_socios=total_socios, activos=activos, saldo=saldo, prox=prox, impagas=impagas)
    return page(body, title='Dashboard')

# --------------------
# Usuarios (admin)
# --------------------
@app.get('/usuarios')
@login_required
@role_required('admin')
def usuarios():
    us = User.query.order_by(User.username).all()
    body = render("""
    <h3 class="text-oliva">Usuarios</h3>
    <a class="btn btn-sm btn-oliva mb-3" href="{{ url_for('nuevo_usuario') }}">Nuevo</a>
    <table class="table table-striped"><thead><tr><th>Usuario</th><th>Rol</th><th></th></tr></thead><tbody>
    {% for u in us %}
      <tr><td>{{ u.username }}</td><td>{{ u.role }}</td>
        <td class="text-end"><a class="btn btn-sm btn-outline-dark" href="{{ url_for('editar_usuario', uid=u.id) }}">Editar</a></td></tr>
    {% endfor %}</tbody></table>
    """, us=us)
    return page(body, title='Usuarios')

@app.route('/usuarios/nuevo', methods=['GET','POST'])
@login_required
@role_required('admin')
def nuevo_usuario():
    if request.method == 'POST':
        username = request.form['username'].strip()
        role = request.form['role']
        pwd = request.form['password']
        if User.query.filter_by(username=username).first():
            flash('El usuario ya existe'); return redirect(url_for('nuevo_usuario'))
        u = User(username=username, role=role); u.set_password(pwd)
        db.session.add(u); db.session.commit()
        flash('Usuario creado'); return redirect(url_for('usuarios'))
    body = """
    <h3 class="text-oliva">Nuevo usuario</h3>
    <form method="post" class="card"><div class="card-body row g-2">
      <div class="col-md-4"><input class="form-control" name="username" placeholder="Usuario" required></div>
      <div class="col-md-3"><select class="form-select" name="role">
        <option value="admin">admin</option><option value="operador" selected>operador</option><option value="consulta">consulta</option>
      </select></div>
      <div class="col-md-4"><input type="password" class="form-control" name="password" placeholder="Contraseña" required></div>
    </div><div class="card-footer text-end"><button class="btn btn-oliva">Guardar</button></div></form>
    """
    return page(body, title='Nuevo usuario')

@app.route('/usuarios/<int:uid>/editar', methods=['GET','POST'])
@login_required
@role_required('admin')
def editar_usuario(uid):
    u = User.query.get_or_404(uid)
    if request.method == 'POST':
        u.role = request.form['role']
        newpwd = (request.form.get('password') or '').strip()
        if newpwd: u.set_password(newpwd)
        db.session.commit(); flash('Usuario actualizado')
        return redirect(url_for('usuarios'))
    body = render("""
    <h3 class="text-oliva">Editar usuario</h3>
    <form method="post" class="card"><div class="card-body row g-2">
      <div class="col-md-4"><input class="form-control" value="{{ u.username }}" disabled></div>
      <div class="col-md-3"><select class="form-select" name="role">
        <option value="admin" {{ 'selected' if u.role=='admin' else '' }}>admin</option>
        <option value="operador" {{ 'selected' if u.role=='operador' else '' }}>operador</option>
        <option value="consulta" {{ 'selected' if u.role=='consulta' else '' }}>consulta</option>
      </select></div>
      <div class="col-md-4"><input type="password" class="form-control" name="password" placeholder="Nueva contraseña (opcional)"></div>
    </div><div class="card-footer text-end"><button class="btn btn-oliva">Guardar</button></div></form>
    """, u=u)
    return page(body, title='Editar usuario')

# --------------------
# Socios (ABM)
# --------------------
@app.route('/socios', methods=['GET','POST'])
@login_required
def socios():
    if request.method == 'POST':
        s = Socio(
            nombre=request.form['nombre'].strip(),
            email=(request.form.get('email') or '').strip() or None,
            dni=(request.form.get('dni') or '').strip() or None,
            telefono=(request.form.get('telefono') or '').strip() or None,
            activo=bool(request.form.get('activo')),
            cuota_mensual=float(request.form.get('cuota_mensual') or 0)
        )
        db.session.add(s); db.session.commit(); flash('Socio creado'); return redirect(url_for('socios'))

    q = (request.args.get('q') or '').strip()
    qry = Socio.query
    if q:
        like = f"%{q}%"; qry = qry.filter(db.or_(Socio.nombre.ilike(like), Socio.email.ilike(like), Socio.dni.ilike(like)))
    lista = qry.order_by(Socio.nombre.asc()).all()

    body = render("""
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h3 class="text-oliva">Socios</h3>
      <a class="btn btn-sm btn-outline-dark" href="{{ url_for('export_socios') }}">Export CSV</a>
    </div>

    <form class="row g-2 mb-3">
      <div class="col-md-4"><input class="form-control" name="q" value="{{ q }}" placeholder="Buscar por nombre, email o DNI"></div>
      <div class="col-md-2"><button class="btn btn-oliva w-100">Buscar</button></div>
    </form>

    <form method="post" class="card mb-4">
      <div class="card-header">Nuevo socio</div>
      <div class="card-body row g-2">
        <div class="col-md-3"><input class="form-control" name="nombre" placeholder="Nombre y apellido" required></div>
        <div class="col-md-3"><input class="form-control" name="email" placeholder="Email"></div>
        <div class="col-md-2"><input class="form-control" name="dni" placeholder="DNI"></div>
        <div class="col-md-2"><input class="form-control" name="telefono" placeholder="Teléfono (E.164 p/WhatsApp)"></div>
        <div class="col-md-2"><input class="form-control" name="cuota_mensual" type="number" step="0.01" min="0" placeholder="Cuota $"></div>
        <div class="col-md-1 form-check mt-2">
          <input class="form-check-input" type="checkbox" name="activo" id="activo" checked>
          <label class="form-check-label" for="activo">Activo</label>
        </div>
      </div>
      <div class="card-footer text-end"><button class="btn btn-oliva">Guardar</button></div>
    </form>

    <div class="table-responsive"><table class="table table-striped align-middle">
      <thead><tr><th>Nombre</th><th>Email</th><th>DNI</th><th>Teléfono</th><th>Cuota $</th><th>Activo</th><th></th></tr></thead><tbody>
      {% for s in lista %}
        <tr>
          <td>{{ s.nombre }}</td><td>{{ s.email or '—' }}</td><td>{{ s.dni or '—' }}</td><td>{{ s.telefono or '—' }}</td>
          <td>${{ '%.2f' % s.cuota_mensual }}</td><td>{{ 'Sí' if s.activo else 'No' }}</td>
          <td class="text-end">
            <a class="btn btn-sm btn-outline-dark" href="{{ url_for('editar_socio', socio_id=s.id) }}">Editar</a>
            <a class="btn btn-sm btn-outline-danger" href="{{ url_for('eliminar_socio', socio_id=s.id) }}" onclick="return confirm('¿Eliminar socio?')">Eliminar</a>
          </td>
        </tr>
      {% endfor %}</tbody></table></div>
    """, lista=lista, q=q)
    return page(body, title='Socios')

@app.route('/socios/<int:socio_id>/editar', methods=['GET','POST'])
@login_required
def editar_socio(socio_id):
    s = Socio.query.get_or_404(socio_id)
    if request.method == 'POST':
        s.nombre = request.form['nombre'].strip()
        s.email = (request.form.get('email') or '').strip() or None
        s.dni = (request.form.get('dni') or '').strip() or None
        s.telefono = (request.form.get('telefono') or '').strip() or None
        s.activo = bool(request.form.get('activo'))
        s.cuota_mensual = float(request.form.get('cuota_mensual') or 0)
        db.session.commit(); flash('Socio actualizado'); return redirect(url_for('socios'))
    body = render("""
    <h3 class="text-oliva">Editar socio</h3>
    <form method="post" class="card"><div class="card-body row g-2">
      <div class="col-md-4"><input class="form-control" name="nombre" value="{{ s.nombre }}" required></div>
      <div class="col-md-3"><input class="form-control" name="email" value="{{ s.email or '' }}"></div>
      <div class="col-md-2"><input class="form-control" name="dni" value="{{ s.dni or '' }}"></div>
      <div class="col-md-2"><input class="form-control" name="telefono" value="{{ s.telefono or '' }}"></div>
      <div class="col-md-2"><input class="form-control" name="cuota_mensual" type="number" step="0.01" min="0" value="{{ '%.2f' % s.cuota_mensual }}"></div>
      <div class="col-md-1 form-check mt-2">
        <input class="form-check-input" type="checkbox" name="activo" id="activo" {{ 'checked' if s.activo else '' }}>
        <label class="form-check-label" for="activo">Activo</label>
      </div>
    </div><div class="card-footer d-flex gap-2">
      <a class="btn btn-secondary" href="{{ url_for('socios') }}">Volver</a>
      <button class="btn btn-oliva">Guardar</button></div></form>
    """, s=s)
    return page(body, title='Editar socio')

@app.get('/socios/<int:socio_id>/eliminar')
@login_required
def eliminar_socio(socio_id):
    s = Socio.query.get_or_404(socio_id)
    db.session.delete(s); db.session.commit()
    flash('Socio eliminado')
    return redirect(url_for('socios'))

@app.get('/export/socios.csv')
@login_required
def export_socios():
    si = StringIO(); w = csv.writer(si)
    w.writerow(['id','nombre','email','dni','telefono','activo','fecha_alta','cuota_mensual'])
    for s in Socio.query.order_by(Socio.id.asc()).all():
        w.writerow([s.id, s.nombre, s.email or '', s.dni or '', s.telefono or '', int(s.activo), s.fecha_alta, s.cuota_mensual])
    return Response(si.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition':'attachment; filename=socios.csv'})

# --------------------
# Movimientos (Libro único) + Filtros + Comprobante texto
# --------------------
@app.route('/movimientos', methods=['GET','POST'])
@login_required
def movimientos():
    if request.method == 'POST':
        tipo = request.form['tipo']  # ingreso | salida
        if tipo not in ('ingreso', 'salida'):
            flash('Tipo inválido'); return redirect(url_for('movimientos'))
        categoria = (request.form.get('categoria') or '').strip() or None
        monto = float(request.form['monto'])
        fecha_mov = datetime.strptime(request.form['fecha'], '%Y-%m-%d').date() if request.form.get('fecha') else date.today()
        descripcion = (request.form.get('descripcion') or '').strip() or None
        comp_tipo = (request.form.get('comp_tipo') or '').strip() or None
        comp_nro  = (request.form.get('comp_nro') or '').strip() or None

        m = Movimiento(tipo=tipo, categoria=categoria, monto=monto, fecha=fecha_mov,
                       descripcion=descripcion, comp_tipo=comp_tipo, comp_nro=comp_nro)
        db.session.add(m)
        db.session.commit()
        flash('Movimiento registrado')
        return redirect(url_for('movimientos'))

    # Filtros
    qry = Movimiento.query
    t = (request.args.get('tipo') or '').strip()
    o = (request.args.get('origen') or '').strip()
    cat = (request.args.get('categoria') or '').strip()
    d1 = (request.args.get('desde') or '').strip()
    d2 = (request.args.get('hasta') or '').strip()

    if t in ('ingreso','salida'): qry = qry.filter(Movimiento.tipo == t)
    if o: qry = qry.filter(Movimiento.origen == o)
    if cat:
        like = f"%{cat}%"; qry = qry.filter(Movimiento.categoria.ilike(like))
    if d1:
        from datetime import datetime as dt
        qry = qry.filter(Movimiento.fecha >= dt.strptime(d1, '%Y-%m-%d').date())
    if d2:
        from datetime import datetime as dt
        qry = qry.filter(Movimiento.fecha <= dt.strptime(d2, '%Y-%m-%d').date())

    lista = qry.order_by(Movimiento.fecha.desc(), Movimiento.id.desc()).all()
    ingresos = sum(m.monto for m in lista if m.tipo == 'ingreso')
    salidas  = sum(m.monto for m in lista if m.tipo == 'salida')
    saldo = ingresos - salidas

    body = render("""
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h3 class="text-oliva">Ingresos y Salidas</h3>
      <a class="btn btn-sm btn-outline-dark" href="{{ url_for('export_movimientos') }}">Export CSV</a>
    </div>

    <!-- Filtros -->
    <form class="row g-2 mb-3">
      <div class="col-md-2">
        <select name="tipo" class="form-select">
          <option value="">Todos</option>
          <option value="ingreso" {{ 'selected' if request.args.get('tipo')=='ingreso' else '' }}>Ingresos</option>
          <option value="salida"  {{ 'selected' if request.args.get('tipo')=='salida'  else '' }}>Salidas</option>
        </select>
      </div>
      <div class="col-md-3">
        <select name="origen" class="form-select">
          {% for o in ['', 'manual','cuota','merch','evento','escuela'] %}
            <option value="{{ o }}" {{ 'selected' if request.args.get('origen')==o else '' }}>
              {{ o or 'Todos los orígenes' }}
            </option>
          {% endfor %}
        </select>
      </div>
      <div class="col-md-2"><input type="date" name="desde" class="form-control" value="{{ request.args.get('desde','') }}"></div>
      <div class="col-md-2"><input type="date" name="hasta" class="form-control" value="{{ request.args.get('hasta','') }}"></div>
      <div class="col-md-2"><input class="form-control" name="categoria" value="{{ request.args.get('categoria','') }}" placeholder="Categoría"></div>
      <div class="col-md-1"><button class="btn btn-oliva w-100">Filtrar</button></div>
    </form>

    <form method="post" class="card mb-4">
      <div class="card-header">Nuevo movimiento</div>
      <div class="card-body row g-2">
        <div class="col-md-2">
          <select name="tipo" class="form-select" required>
            <option value="ingreso">Ingreso</option>
            <option value="salida">Salida</option>
          </select>
        </div>
        <div class="col-md-3">
          <select class="form-select" name="categoria">
            <optgroup label="Ingresos">
              {% for c in INGRESO_CATS %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
            </optgroup>
            <optgroup label="Salidas">
              {% for c in SALIDA_CATS %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
            </optgroup>
          </select>
        </div>
        <div class="col-md-2"><input class="form-control" name="monto" type="number" step="0.01" min="0" placeholder="Monto" required></div>
        <div class="col-md-2"><input class="form-control" name="fecha" type="date" value="{{ hoy }}"></div>
        <div class="col-md-3"><input class="form-control" name="descripcion" placeholder="Descripción"></div>
        <div class="col-md-2"><input class="form-control" name="comp_tipo" placeholder="Tipo comp. (Recibo/Factura)"></div>
        <div class="col-md-2"><input class="form-control" name="comp_nro"  placeholder="N° comp."></div>
      </div>
      <div class="card-footer text-end"><button class="btn btn-oliva">Guardar</button></div>
    </form>

    <div class="row g-3 mb-2">
      <div class="col-md-3"><div class="card"><div class="card-body"><small class="text-muted">Total ingresos (filtrados)</small><div class="fs-4">$ {{ '%.2f' % ingresos }}</div></div></div></div>
      <div class="col-md-3"><div class="card"><div class="card-body"><small class="text-muted">Total salidas (filtradas)</small><div class="fs-4">$ {{ '%.2f' % salidas }}</div></div></div></div>
      <div class="col-md-3"><div class="card"><div class="card-body"><small class="text-muted">Saldo</small><div class="fs-4">$ {{ '%.2f' % saldo }}</div></div></div></div>
    </div>

    <div class="table-responsive"><table class="table table-striped align-middle">
      <thead><tr>
        <th>Fecha</th><th>Ingreso/Salida</th><th>Origen</th><th>Categoría</th>
        <th>Descripción</th><th>Comprobante</th><th class="text-end">Monto</th><th></th>
      </tr></thead><tbody>
      {% for m in lista %}
        <tr>
          <td>{{ m.fecha.strftime('%d/%m/%Y') }}</td>
          <td>{{ m.tipo }}</td>
          <td><span class="badge text-bg-light">{{ m.origen or 'manual' }}</span></td>
          <td>{{ m.categoria or '—' }}</td>
          <td>{{ m.descripcion or '—' }}</td>
          <td>
            {% if m.comp_tipo or m.comp_nro %}
              {{ (m.comp_tipo or '') ~ (' ' if m.comp_tipo and m.comp_nro else '') ~ (m.comp_nro or '') }}
            {% else %}—{% endif %}
          </td>
          <td class="text-end">$ {{ '%.2f' % m.monto }}</td>
          <td class="text-end"><a class="btn btn-sm btn-outline-danger" href="{{ url_for('eliminar_movimiento', mov_id=m.id) }}" onclick="return confirm('¿Eliminar movimiento?')">Eliminar</a></td>
        </tr>
      {% endfor %}</tbody></table></div>
    """, lista=lista, ingresos=ingresos, salidas=salidas, saldo=saldo, hoy=date.today().strftime('%Y-%m-%d'),
       INGRESO_CATS=INGRESO_CATS, SALIDA_CATS=SALIDA_CATS)
    return page(body, title='Movimientos')

@app.get('/movimientos/<int:mov_id>/eliminar')
@login_required
def eliminar_movimiento(mov_id):
    m = Movimiento.query.get_or_404(mov_id)
    for comp in m.comprobantes:
        try: os.remove(comp.filename)
        except Exception: pass
        db.session.delete(comp)
    db.session.delete(m); db.session.commit()
    flash('Movimiento eliminado')
    return redirect(url_for('movimientos'))

@app.get('/export/movimientos.csv')
@login_required
def export_movimientos():
    si = StringIO(); w = csv.writer(si)
    w.writerow(['id','tipo','categoria','origen','socio_id','cuota_id','stockmov_id','monto','fecha','descripcion','comp_tipo','comp_nro'])
    for m in Movimiento.query.order_by(Movimiento.id.asc()).all():
        w.writerow([m.id, m.tipo, m.categoria or '', m.origen or 'manual',
                    m.socio_id or '', m.cuota_id or '', m.stockmov_id or '',
                    m.monto, m.fecha, m.descripcion or '', m.comp_tipo or '', m.comp_nro or ''])
    return Response(si.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition':'attachment; filename=movimientos.csv'})

# --------------------
# Eventos + Inscripciones
# --------------------
@app.route('/eventos', methods=['GET','POST'])
@login_required
def eventos():
    if request.method == 'POST':
        e = Evento(
            titulo=request.form['titulo'].strip(),
            fecha=datetime.strptime(request.form['fecha'], '%Y-%m-%d').date(),
            lugar=(request.form.get('lugar') or '').strip() or None,
            descripcion=(request.form.get('descripcion') or '').strip() or None
        )
        db.session.add(e); db.session.commit(); flash('Evento creado'); return redirect(url_for('eventos'))
    evs = Evento.query.order_by(Evento.fecha.desc()).all()
    socios = Socio.query.order_by(Socio.nombre.asc()).all()
    body = render("""
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h3 class="text-oliva">Eventos</h3>
    </div>
    <form method="post" class="card mb-4"><div class="card-header">Nuevo evento</div>
      <div class="card-body row g-2">
        <div class="col-md-4"><input class="form-control" name="titulo" placeholder="Título" required></div>
        <div class="col-md-2"><input class="form-control" type="date" name="fecha" required></div>
        <div class="col-md-3"><input class="form-control" name="lugar" placeholder="Lugar (opcional)"></div>
        <div class="col-md-3"><input class="form-control" name="descripcion" placeholder="Descripción (opcional)"></div>
      </div><div class="card-footer text-end"><button class="btn btn-oliva">Guardar</button></div></form>

    {% for e in evs %}
      <div class="card mb-3">
        <div class="card-header d-flex justify-content-between align-items-center">
          <div><strong>{{ e.titulo }}</strong> — {{ e.fecha.strftime('%d/%m/%Y') }} @ {{ e.lugar or '—' }}</div>
          <a class="btn btn-sm btn-outline-danger" href="{{ url_for('eliminar_evento', evento_id=e.id) }}" onclick="return confirm('¿Eliminar evento?')">Eliminar</a>
        </div>
        <div class="card-body">
          <form method="post" action="{{ url_for('inscribir', evento_id=e.id) }}" class="row g-2 mb-3">
            <div class="col-md-6">
              <select class="form-select" name="socio_id" required>
                <option value="">Inscribir socio…</option>
                {% for s in socios %}<option value="{{ s.id }}">{{ s.nombre }}</option>{% endfor %}
              </select>
            </div>
            <div class="col-md-2"><button class="btn btn-oliva">Inscribir</button></div>
          </form>
          {% set insc = Inscripcion.query.filter_by(evento_id=e.id).all() %}
          {% if insc %}
            <div class="table-responsive"><table class="table table-sm">
              <thead><tr><th>Socio</th><th>Fecha</th><th></th></tr></thead><tbody>
              {% for i in insc %}{% set s = Socio.query.get(i.socio_id) %}
                <tr><td>{{ s.nombre if s else ('#'+i.socio_id|string) }}</td>
                  <td>{{ i.fecha.strftime('%d/%m/%Y') }}</td>
                  <td class="text-end"><a class="btn btn-sm btn-outline-danger" href="{{ url_for('eliminar_inscripcion', insc_id=i.id) }}" onclick="return confirm('¿Quitar inscripción?')">Quitar</a></td></tr>
              {% endfor %}</tbody></table></div>
          {% else %}<em>Sin inscriptos.</em>{% endif %}
        </div>
      </div>
    {% endfor %}
    """, evs=evs, socios=socios, Inscripcion=Inscripcion, Socio=Socio)
    return page(body, title='Eventos')

@app.post('/eventos/<int:evento_id>/inscribir')
@login_required
def inscribir(evento_id):
    _ = Evento.query.get_or_404(evento_id)
    socio_id = int(request.form['socio_id'])
    if not Socio.query.get(socio_id):
        flash('Socio inexistente'); return redirect(url_for('eventos'))
    if Inscripcion.query.filter_by(evento_id=evento_id, socio_id=socio_id).first():
        flash('El socio ya está inscripto'); return redirect(url_for('eventos'))
    db.session.add(Inscripcion(evento_id=evento_id, socio_id=socio_id)); db.session.commit()
    flash('Inscripción registrada'); return redirect(url_for('eventos'))

@app.get('/eventos/<int:evento_id>/eliminar')
@login_required
def eliminar_evento(evento_id):
    e = Evento.query.get_or_404(evento_id)
    Inscripcion.query.filter_by(evento_id=e.id).delete()
    db.session.delete(e); db.session.commit()
    flash('Evento eliminado'); return redirect(url_for('eventos'))

@app.get('/inscripciones/<int:insc_id>/eliminar')
@login_required
def eliminar_inscripcion(insc_id):
    i = Inscripcion.query.get_or_404(insc_id)
    db.session.delete(i); db.session.commit()
    flash('Inscripción eliminada'); return redirect(url_for('eventos'))

# --------------------
# Cuotas y Morosidad
# --------------------
@app.route('/cuotas', methods=['GET','POST'])
@login_required
def cuotas():
    if request.method == 'POST':
        periodo = request.form['periodo']  # 'YYYY-MM'
        y, m = map(int, periodo.split('-'))
        venc = date(y, m, min(10, ultimo_dia_mes(y, m)))  # vencimiento día 10
        activos = Socio.query.filter(Socio.activo==True, Socio.cuota_mensual>0).all()
        creadas = 0
        for s in activos:
            if not Cuota.query.filter_by(socio_id=s.id, periodo=periodo).first():
                db.session.add(Cuota(socio_id=s.id, periodo=periodo, monto=s.cuota_mensual, fecha_venc=venc))
                creadas += 1
        db.session.commit()
        flash(f'Cuotas generadas: {creadas} para {periodo}')
        return redirect(url_for('cuotas', periodo=periodo))

    periodo = (request.args.get('periodo') or date.today().strftime('%Y-%m'))
    q = Cuota.query.filter(Cuota.periodo==periodo).order_by(Cuota.pagada.asc(), Cuota.id.asc()).all()
    socios = {s.id: s.nombre for s in Socio.query.with_entities(Socio.id, Socio.nombre).all()}
    total = sum(c.monto for c in q); cobradas = sum(c.monto for c in q if c.pagada); saldo = total - cobradas

    body = render("""
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h3 class="text-oliva">Cuotas</h3>
      <form method="get" class="d-flex gap-2">
        <input class="form-control" name="periodo" value="{{ periodo }}" placeholder="YYYY-MM" style="width:140px">
        <button class="btn btn-outline-dark">Ver</button>
      </form>
    </div>

    <form method="post" class="card mb-3">
      <div class="card-body d-flex flex-wrap gap-2 align-items-end">
        <div><label class="form-label">Generar período</label>
          <input class="form-control" name="periodo" value="{{ periodo }}" placeholder="YYYY-MM" style="width:140px" required>
        </div>
        <button class="btn btn-oliva">Generar cuotas</button>
        <div class="ms-auto">
          <span class="badge text-bg-light">Total: $ {{ '%.2f' % total }}</span>
          <span class="badge text-bg-success">Cobradas: $ {{ '%.2f' % cobradas }}</span>
          <span class="badge text-bg-warning">Pendiente: $ {{ '%.2f' % saldo }}</span>
        </div>
      </div>
    </form>

    <div class="table-responsive"><table class="table table-striped align-middle">
      <thead><tr><th>Socio</th><th>Período</th><th>Venc.</th><th class="text-end">Monto</th><th>Estado</th><th>Pago/Comprobante</th></tr></thead><tbody>
      {% for c in q %}
        <tr>
          <td>{{ socios.get(c.socio_id, '#'+c.socio_id|string) }}</td>
          <td>{{ c.periodo }}</td>
          <td>{{ c.fecha_venc.strftime('%d/%m/%Y') }}</td>
          <td class="text-end">$ {{ '%.2f' % c.monto }}</td>
          <td>
            {% if c.pagada %}<span class="badge text-bg-success">Pagada</span>
            {% elif c.fecha_venc < hoy %}<span class="badge badge-rojo">Vencida</span>
            {% else %}<span class="badge text-bg-warning">Pendiente</span>{% endif %}
          </td>
          <td class="text-end">
            {% if not c.pagada %}
              <form method="post" action="{{ url_for('pagar_cuota', cuota_id=c.id, periodo=periodo) }}" enctype="multipart/form-data" style="display:inline">
                <input type="file" name="comprobante" accept=".pdf,.jpg,.jpeg,.png" style="max-width:220px">
                <button class="btn btn-sm btn-oliva">Marcar pagada</button>
              </form>
            {% else %}
              {% if c.comprobantes %}
                <a class="btn btn-sm btn-outline-dark" href="{{ url_for('ver_comprobante', cid=c.comprobantes[0].id) }}" target="_blank">Ver comp.</a>
              {% endif %}
              <a class="btn btn-sm btn-outline-secondary" href="{{ url_for('revertir_pago', cuota_id=c.id, periodo=periodo) }}">Revertir</a>
            {% endif %}
          </td>
        </tr>
      {% endfor %}
      </tbody></table></div>
    """, q=q, socios=socios, periodo=periodo, total=total, cobradas=cobradas, saldo=saldo, hoy=date.today())
    return page(body, title='Cuotas')

@app.get('/comprobante/<int:cid>')
@login_required
def ver_comprobante(cid):
    c = Comprobante.query.get_or_404(cid)
    return send_file(c.filename, as_attachment=False)

@app.post('/cuotas/<int:cuota_id>/pagar')
@login_required
def pagar_cuota(cuota_id):
    c = Cuota.query.get_or_404(cuota_id)
    c.pagada = True; c.fecha_pago = date.today()
    # Ingreso con trazabilidad (libro único)
    db.session.add(Movimiento(
        tipo='ingreso',
        categoria='Cuotas socios',
        origen='cuota',
        socio_id=c.socio_id,
        cuota_id=c.id,
        monto=c.monto,
        fecha=date.today(),
        descripcion=f'Cuota {c.periodo} socio #{c.socio_id}'
    ))
    db.session.flush()
    f = request.files.get('comprobante')
    if f and f.filename and allowed_file(f.filename):
        fn = secure_filename(f.filename)
        dst = os.path.join(app.config['UPLOAD_FOLDER'], f"cuota_{c.id}_{fn}")
        f.save(dst)
        db.session.add(Comprobante(filename=dst.replace('\\','/'), cuota_id=c.id))
    db.session.commit()
    flash('Cuota marcada como pagada')
    return redirect(url_for('cuotas', periodo=request.args.get('periodo')))

@app.get('/cuotas/<int:cuota_id>/revertir')
@login_required
def revertir_pago(cuota_id):
    c = Cuota.query.get_or_404(cuota_id)
    for comp in c.comprobantes:
        try: os.remove(comp.filename)
        except Exception: pass
        db.session.delete(comp)
    mov = Movimiento.query.filter_by(cuota_id=c.id, origen='cuota').order_by(Movimiento.id.desc()).first()
    if mov:
        for comp in mov.comprobantes:
            try: os.remove(comp.filename)
            except Exception: pass
            db.session.delete(comp)
        db.session.delete(mov)
    c.pagada = False; c.fecha_pago = None
    db.session.commit(); flash('Pago revertido')
    return redirect(url_for('cuotas', periodo=request.args.get('periodo')))

@app.get('/morosidad')
@login_required
def morosidad():
    hoy = date.today()
    vencidas = Cuota.query.filter(Cuota.pagada==False, Cuota.fecha_venc < hoy).all()
    deudas = {}
    for c in vencidas:
        deudas.setdefault(c.socio_id, {'monto':0.0,'cuotas':[]})
        deudas[c.socio_id]['monto'] += c.monto
        deudas[c.socio_id]['cuotas'].append(c)
    socios_map = {s.id: s.nombre for s in Socio.query.with_entities(Socio.id, Socio.nombre).all()}
    total = sum(v['monto'] for v in deudas.values())

    body = render("""
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h3 class="text-oliva">Morosidad</h3>
      <div class="d-flex gap-2">
        <form method="post" action="{{ url_for('enviar_recordatorios') }}">
          <input type="hidden" name="via" value="email"><button class="btn btn-sm btn-oliva">Recordar por Email</button>
        </form>
        <form method="post" action="{{ url_for('enviar_recordatorios') }}">
          <input type="hidden" name="via" value="whatsapp"><button class="btn btn-sm btn-outline-dark">Recordar por WhatsApp</button>
        </form>
      </div>
    </div>

    <div class="mb-3"><span class="badge text-bg-danger">Total adeudado: $ {{ '%.2f' % total }}</span></div>

    {% if not deudas %}<div class="alert alert-success">No hay cuotas vencidas.</div>
    {% else %}
      {% for sid, info in deudas.items() %}
        <div class="card mb-3">
          <div class="card-header d-flex justify-content-between">
            <strong>{{ socios_map.get(sid, '#'+sid|string) }}</strong>
            <span>Deuda: <strong>$ {{ '%.2f' % info.monto }}</strong></span>
          </div>
          <div class="card-body table-responsive">
            <table class="table table-sm align-middle mb-0">
              <thead><tr><th>Período</th><th>Vencimiento</th><th class="text-end">Monto</th><th></th></tr></thead>
              <tbody>
                {% for c in info.cuotas %}
                  <tr>
                    <td>{{ c.periodo }}</td>
                    <td>{{ c.fecha_venc.strftime('%d/%m/%Y') }}</td>
                    <td class="text-end">$ {{ '%.2f' % c.monto }}</td>
                    <td class="text-end">
                      <a class="btn btn-sm btn-oliva" href="{{ url_for('cuotas', periodo=c.periodo) }}">Gestionar</a>
                    </td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </div>
      {% endfor %}
    {% endif %}
    """, deudas=deudas, socios_map=socios_map, total=total)
    return page(body, title='Morosidad')

@app.post('/morosidad/recordatorios')
@login_required
def enviar_recordatorios():
    via = request.form.get('via')  # 'email' | 'whatsapp'
    hoy = date.today()
    vencidas = Cuota.query.filter(Cuota.pagada==False, Cuota.fecha_venc < hoy).all()

    enviados, errores = 0, []
    for c in vencidas:
        s = Socio.query.get(c.socio_id)
        if not s: continue
        mensaje = (f"Hola {s.nombre},\n\n"
                   f"Tenés cuotas pendientes en la Asociación.\n"
                   f"- Período: {c.periodo}\n- Vencimiento: {c.fecha_venc.strftime('%d/%m/%Y')}\n"
                   f"- Importe: ${c.monto:,.2f}\n\n"
                   "Te pedimos regularizar a la brevedad. Gracias.\nACP Rosario")

        if via == 'email' and s.email:
            ok, info = send_email(s.email, "Recordatorio de cuota pendiente", mensaje)
        elif via == 'whatsapp' and s.telefono:
            ok, info = send_whatsapp(s.telefono, mensaje)  # E.164: +549341...
        else:
            ok, info = False, 'Sin contacto'
        enviados += 1 if ok else 0
        if not ok: errores.append((s.nombre, info))

    flash(f'Recordatorios enviados: {enviados}. Errores: {len(errores)}')
    return redirect(url_for('morosidad'))

# --------------------
# Main
# --------------------
if __name__ == '__main__':
    # Para compartir en LAN:
    # app.run(host='0.0.0.0', port=5000, debug=False)
    app.run(debug=True)
