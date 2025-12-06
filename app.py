#!/usr/bin/env python3
"""
TechStart Solutions Portal
Flask web app demonstrating:
- Azure VM hosting
- Azure SQL Database (CRUD)
- Azure Blob Storage (profile images)
- Role-based access control (Developers, QA, PMs)
"""

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash
)
import os
from functools import wraps
from datetime import datetime

import pyodbc
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")

# ---------- Database helpers ----------

def get_db_connection():
    """Establish connection to Azure SQL Database with TLS."""
    server = os.getenv("SQL_SERVER")
    database = os.getenv("SQL_DATABASE")
    username = os.getenv("SQL_USERNAME")
    password = os.getenv("SQL_PASSWORD")

    connection_string = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )
    return pyodbc.connect(connection_string)


def get_blob_url(blob_name: str | None):
    """Generate public URL for blob in Azure Storage."""
    if not blob_name:
        return None
    conn_str = os.getenv("BLOB_CONNECTION_STRING")
    container_name = os.getenv("BLOB_CONTAINER_NAME")
    storage_account_name = conn_str.split("AccountName=")[1].split(";")[0]
    return f"https://{storage_account_name}.blob.core.windows.net/{container_name}/{blob_name}"


# ---------- Auth decorators ----------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def roles_required(*roles):
    """
    Usage:
    @app.route('/projects/create')
    @login_required
    @roles_required('developer', 'pm')
    """
    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "role" not in session:
                flash("You are not authorised for this action", "error")
                return redirect(url_for("profile"))
            if session["role"] not in roles:
                flash("You do not have permission to do that.", "error")
                return redirect(url_for("profile"))
            return f(*args, **kwargs)
        return decorated
    return wrapper


# ---------- Routes ----------

@app.route("/", methods=["GET", "POST"])
def login():
    """Login page."""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # NOTE: for demo purposes we treat password_hash as plain text
            # In production, use proper hashing!
            query = """
                SELECT user_id, username, full_name, role
                FROM users
                WHERE username = ? AND password_hash = ?
            """
            cursor.execute(query, (username, password))
            user = cursor.fetchone()
            conn.close()

            if user:
                session["user_id"] = user[0]
                session["username"] = user[1]
                session["full_name"] = user[2]
                session["role"] = user[3]
                flash("Login successful!", "success")
                return redirect(url_for("profile"))
            else:
                flash("Invalid username or password", "error")

        except Exception as e:
            flash(f"Database error: {e}", "error")

    return render_template("login.html")


@app.route("/profile")
@login_required
def profile():
    """Profile view with data from SQL + profile image from Blob."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = """
            SELECT username, full_name, email, department,
                   created_date, profile_image_url, role
            FROM users
            WHERE user_id = ?
        """
        cursor.execute(query, (session["user_id"],))
        user = cursor.fetchone()
        conn.close()

        if not user:
            flash("User not found", "error")
            return redirect(url_for("logout"))

        user_data = {
            "username": user[0],
            "full_name": user[1],
            "email": user[2],
            "department": user[3],
            "created_date": (
                user[4].strftime("%Y-%m-%d") if user[4] else "N/A"
            ),
            "profile_image": get_blob_url(user[5]),
            "role": user[6],
        }
        return render_template("profile.html", user=user_data)

    except Exception as e:
        flash(f"Error loading profile: {e}", "error")
        return redirect(url_for("logout"))


# ---------- Projects CRUD ----------

@app.route("/projects")
@login_required
def projects_list():
    """List all projects (read)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT project_id, name, description,
                   owner_username, status, created_at
            FROM projects
            ORDER BY created_at DESC
        """)
        rows = cursor.fetchall()
        conn.close()

        projects = [
            {
                "id": r[0],
                "name": r[1],
                "description": r[2],
                "owner": r[3],
                "status": r[4],
                "created_at": r[5].strftime("%Y-%m-%d") if r[5] else "",
            }
            for r in rows
        ]

        return render_template(
            "projects.html",
            projects=projects,
            current_role=session.get("role"),
        )
    except Exception as e:
        flash(f"Error loading projects: {e}", "error")
        return render_template("projects.html", projects=[], current_role=session.get("role"))


@app.route("/projects/create", methods=["GET", "POST"])
@login_required
@roles_required("developer", "pm")
def project_create():
    """Create a new project (create)."""
    if request.method == "POST":
        name = request.form.get("name")
        description = request.form.get("description")
        status = request.form.get("status", "New")
        owner = session.get("username")

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO projects (name, description, owner_username, status, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (name, description, owner, status, datetime.utcnow()))
            conn.commit()
            conn.close()
            flash("Project created successfully", "success")
            return redirect(url_for("projects_list"))
        except Exception as e:
            flash(f"Error creating project: {e}", "error")

    return render_template("projects_form.html", action="Create")


@app.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required("developer", "pm")
def project_edit(project_id):
    """Update an existing project (update)."""
    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        name = request.form.get("name")
        description = request.form.get("description")
        status = request.form.get("status")

        try:
            cursor.execute("""
                UPDATE projects
                SET name = ?, description = ?, status = ?
                WHERE project_id = ?
            """, (name, description, status, project_id))
            conn.commit()
            conn.close()
            flash("Project updated successfully", "success")
            return redirect(url_for("projects_list"))
        except Exception as e:
            conn.close()
            flash(f"Error updating project: {e}", "error")
            return redirect(url_for("projects_list"))

    # GET – load existing data
    cursor.execute("""
        SELECT project_id, name, description, status
        FROM projects
        WHERE project_id = ?
    """, (project_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        flash("Project not found", "error")
        return redirect(url_for("projects_list"))

    project = {
        "id": row[0],
        "name": row[1],
        "description": row[2],
        "status": row[3],
    }

    return render_template(
        "projects_form.html", action="Edit", project=project
    )


@app.route("/projects/<int:project_id>/delete", methods=["POST"])
@login_required
@roles_required("developer")
def project_delete(project_id):
    """Delete a project (delete). Only developers allowed."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        conn.commit()
        conn.close()
        flash("Project deleted", "success")
    except Exception as e:
        flash(f"Error deleting project: {e}", "error")
    return redirect(url_for("projects_list"))


@app.route("/logout")
def logout():
    """Logout."""
    session.clear()
    flash("You have been logged out", "info")
    return redirect(url_for("login"))


if __name__ == "__main__":
    # In production use gunicorn + nginx, debug False
    app.run(host="0.0.0.0", port=5000, debug=False)
