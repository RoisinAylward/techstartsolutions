import os
import uuid
from datetime import datetime, timedelta

import pyodbc
from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, abort, send_from_directory
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Azure SDKs (install: azure-identity azure-keyvault-secrets azure-storage-blob)
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")

# ---- Config (no secrets here) ----
KEY_VAULT_NAME = os.getenv("KEY_VAULT_NAME")  # e.g., "techstart-kv"
SQL_SECRET_NAME = os.getenv("SQL_SECRET_NAME", "sql-connection-string")
STORAGE_SECRET_NAME = os.getenv("STORAGE_SECRET_NAME", "storage-connection-string")
BLOB_CONTAINER = os.getenv("BLOB_CONTAINER_NAME", "app-files")

UPLOAD_MAX_MB = int(os.getenv("UPLOAD_MAX_MB", "5"))
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "pdf", "txt"}

# ---- Key Vault / Secret helpers ----
_credential = None
_secret_client = None
_secrets_cache = {}

def _get_secret_client():
    global _credential, _secret_client
    if _secret_client is not None:
        return _secret_client
    if not KEY_VAULT_NAME:
        return None
    vault_url = f"https://{KEY_VAULT_NAME}.vault.azure.net"
    _credential = DefaultAzureCredential()
    _secret_client = SecretClient(vault_url=vault_url, credential=_credential)
    return _secret_client

def get_secret(secret_name: str) -> str | None:
    # cache in-memory (WSGI workers will each cache separately)
    if secret_name in _secrets_cache:
        return _secrets_cache[secret_name]
    # allow local override for testing
    env_key = f"SECRET_{secret_name.upper().replace('-', '_')}"
    if os.getenv(env_key):
        _secrets_cache[secret_name] = os.getenv(env_key)
        return _secrets_cache[secret_name]

    client = _get_secret_client()
    if client is None:
        return None
    val = client.get_secret(secret_name).value
    _secrets_cache[secret_name] = val
    return val

# ---- DB helpers ----
def db_connect():
    """
    Uses Key Vault secret `sql-connection-string` by default.
    Example secret value:
      Driver={ODBC Driver 18 for SQL Server};Server=tcp:...;Database=...;Uid=...;Pwd=...;Encrypt=yes;TrustServerCertificate=no;
    """
    conn_str = get_secret(SQL_SECRET_NAME) or os.getenv("SQL_CONNECTION_STRING")
    if not conn_str:
        raise RuntimeError("No SQL connection string. Set KEY_VAULT_NAME and secret, or SQL_CONNECTION_STRING env var.")
    return pyodbc.connect(conn_str, timeout=10)

def db_exec(query: str, params=()):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()

def db_fetchall(query: str, params=()):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows

def db_fetchone(query: str, params=()):
    rows = db_fetchall(query, params)
    return rows[0] if rows else None

# ---- Blob helpers (private container + SAS for viewing) ----
_blob_service = None

def _parse_storage_account_name(conn_str: str) -> str | None:
    # "AccountName=xxx;"
    for part in conn_str.split(";"):
        if part.lower().startswith("accountname="):
            return part.split("=", 1)[1]
    return None

def blob_service():
    global _blob_service
    if _blob_service is not None:
        return _blob_service
    conn_str = get_secret(STORAGE_SECRET_NAME) or os.getenv("STORAGE_CONNECTION_STRING")
    if not conn_str:
        raise RuntimeError("No Storage connection string. Set KEY_VAULT_NAME and secret, or STORAGE_CONNECTION_STRING env var.")
    _blob_service = BlobServiceClient.from_connection_string(conn_str)
    return _blob_service

def upload_to_blob(file_storage):
    filename = secure_filename(file_storage.filename or "")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
        raise ValueError(f"File type not allowed: {ext}")
    # Unique blob name
    blob_name = f"uploads/{uuid.uuid4().hex}_{filename}"
    client = blob_service().get_blob_client(container=BLOB_CONTAINER, blob=blob_name)
    client.upload_blob(file_storage.stream, overwrite=True)
    return blob_name

def blob_sas_url(blob_name: str, minutes=15) -> str:
    # Uses account key embedded in connection string (kept in Key Vault)
    conn_str = get_secret(STORAGE_SECRET_NAME) or os.getenv("STORAGE_CONNECTION_STRING")
    account_name = _parse_storage_account_name(conn_str or "")
    if not account_name:
        # best-effort fallback
        account_name = blob_service().account_name

    # Get account key from conn string
    account_key = None
    for part in (conn_str or "").split(";"):
        if part.lower().startswith("accountkey="):
            account_key = part.split("=", 1)[1]
            break
    if not account_key:
        raise RuntimeError("AccountKey missing from storage connection string.")

    sas = generate_blob_sas(
        account_name=account_name,
        container_name=BLOB_CONTAINER,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(minutes=minutes),
    )
    return f"https://{account_name}.blob.core.windows.net/{BLOB_CONTAINER}/{blob_name}?{sas}"

# ---- Auth / RBAC ----
def login_required(fn):
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper

def role_required(*allowed_roles):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            user = session.get("user")
            if not user:
                return redirect(url_for("login"))
            if user.get("role") not in allowed_roles:
                abort(403)
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator

# ---- Routes ----
@app.route("/")
def index():
    if "user" in session:
        return redirect(url_for("tickets"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = db_fetchone(
            "SELECT user_id, username, full_name, role, password_hash FROM dbo.users WHERE username = ?",
            (username,),
        )
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid username or password.", "error")
            return render_template("login.html")

        session["user"] = {
            "user_id": user["user_id"],
            "username": user["username"],
            "full_name": user["full_name"],
            "role": user["role"],
        }
        return redirect(url_for("profile"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/profile")
@login_required
def profile():
    return render_template("profile.html", user=session["user"])

@app.route("/tickets")
@login_required
def tickets():
    rows = db_fetchall("""
        SELECT t.ticket_id, t.title, t.status, t.created_at,
               u.username AS created_by, t.attachment_blob
        FROM dbo.tickets t
        JOIN dbo.users u ON u.user_id = t.created_by
        ORDER BY t.ticket_id DESC
    """)
    return render_template("tickets.html", user=session["user"], tickets=rows)

@app.route("/tickets/<int:ticket_id>")
@login_required
def ticket_view(ticket_id):
    row = db_fetchone("""
        SELECT t.ticket_id, t.title, t.description, t.status, t.created_at,
               u.username AS created_by, t.attachment_blob
        FROM dbo.tickets t
        JOIN dbo.users u ON u.user_id = t.created_by
        WHERE t.ticket_id = ?
    """, (ticket_id,))
    if not row:
        abort(404)
    attachment_url = blob_sas_url(row["attachment_blob"]) if row.get("attachment_blob") else None
    return render_template("ticket_view.html", user=session["user"], ticket=row, attachment_url=attachment_url)

@app.route("/tickets/create", methods=["GET", "POST"])
@login_required
@role_required("developer")
def ticket_create():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        status = request.form.get("status", "open").strip()

        if not title:
            flash("Title is required.", "error")
            return render_template("ticket_form.html", mode="create", ticket={"status": status})

        attachment_blob = None
        f = request.files.get("attachment")
        if f and f.filename:
            # size check (Werkzeug doesn't always stream size; quick guard using content_length)
            if request.content_length and request.content_length > UPLOAD_MAX_MB * 1024 * 1024:
                flash(f"Upload too large (max {UPLOAD_MAX_MB} MB).", "error")
                return render_template("ticket_form.html", mode="create", ticket={"title": title, "description": description, "status": status})
            attachment_blob = upload_to_blob(f)

        with db_connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO dbo.tickets (title, description, status, created_by, attachment_blob)
                VALUES (?, ?, ?, ?, ?)
            """, (title, description, status, session["user"]["user_id"], attachment_blob))
            conn.commit()

        flash("Ticket created.", "success")
        return redirect(url_for("tickets"))

    return render_template("ticket_form.html", mode="create", ticket={"status": "open"})

@app.route("/tickets/<int:ticket_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("developer")
def ticket_edit(ticket_id):
    row = db_fetchone("SELECT ticket_id, title, description, status, attachment_blob FROM dbo.tickets WHERE ticket_id = ?", (ticket_id,))
    if not row:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        status = request.form.get("status", "open").strip()

        attachment_blob = row.get("attachment_blob")
        f = request.files.get("attachment")
        if f and f.filename:
            attachment_blob = upload_to_blob(f)

        db_exec("""
            UPDATE dbo.tickets
            SET title = ?, description = ?, status = ?, attachment_blob = ?
            WHERE ticket_id = ?
        """, (title, description, status, attachment_blob, ticket_id))

        flash("Ticket updated.", "success")
        return redirect(url_for("ticket_view", ticket_id=ticket_id))

    return render_template("ticket_form.html", mode="edit", ticket=row)

@app.route("/tickets/<int:ticket_id>/delete", methods=["POST"])
@login_required
@role_required("developer")
def ticket_delete(ticket_id):
    db_exec("DELETE FROM dbo.tickets WHERE ticket_id = ?", (ticket_id,))
    flash("Ticket deleted.", "success")
    return redirect(url_for("tickets"))

# ---- One-time bootstrap helpers (run manually) ----
@app.cli.command("init-db")
def init_db():
    """
    Optional: initialize tables if you want to run via `flask init-db`.
    (You can also run the SQL script provided in the README.)
    """
    db_exec("""
    IF OBJECT_ID('dbo.users','U') IS NULL
    BEGIN
      CREATE TABLE dbo.users (
        user_id INT IDENTITY(1,1) PRIMARY KEY,
        username NVARCHAR(50) NOT NULL UNIQUE,
        full_name NVARCHAR(100) NOT NULL,
        role NVARCHAR(20) NOT NULL CHECK (role IN ('developer','qa','pm')),
        password_hash NVARCHAR(255) NOT NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
      );
    END
    """)
    db_exec("""
    IF OBJECT_ID('dbo.tickets','U') IS NULL
    BEGIN
      CREATE TABLE dbo.tickets (
        ticket_id INT IDENTITY(1,1) PRIMARY KEY,
        title NVARCHAR(200) NOT NULL,
        description NVARCHAR(MAX) NULL,
        status NVARCHAR(20) NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_progress','done')),
        created_by INT NOT NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        attachment_blob NVARCHAR(260) NULL,
        CONSTRAINT FK_tickets_users FOREIGN KEY (created_by) REFERENCES dbo.users(user_id)
      );
    END
    """)
    print("DB init complete.")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
