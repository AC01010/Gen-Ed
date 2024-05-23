# SPDX-FileCopyrightText: 2023 Mark Liffiton <liffiton@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
from sqlite3 import Row

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
)
from werkzeug.wrappers.response import Response

from .auth import get_auth, instructor_required
from .csv import csv_response
from .db import get_db

bp = Blueprint('instructor', __name__, url_prefix="/instructor", template_folder='templates')


def get_queries(class_id: int, user: int | None = None) -> list[Row]:
    db = get_db()

    where_clause = "WHERE roles.class_id=?"
    params = [class_id]

    if user is not None:
        where_clause += " AND users.id=?"
        params += [user]

    queries = db.execute(f"""
        SELECT
            queries.id,
            users.display_name,
            users.email,
            queries.*
        FROM queries
        JOIN users
            ON queries.user_id=users.id
        JOIN roles
            ON queries.role_id=roles.id
        {where_clause}
        ORDER BY query_time DESC
    """, params).fetchall()

    return queries


def get_users(class_id: int, for_export: bool = False) -> list[Row]:
    db = get_db()

    users = db.execute(f"""
        SELECT
            {'roles.id AS role_id,' if not for_export else ''}
            users.id,
            users.display_name,
            users.email,
            auth_providers.name AS auth_provider,
            users.auth_name,
            COUNT(queries.id) AS num_queries,
            SUM(CASE WHEN queries.query_time > date('now', '-7 days') THEN 1 ELSE 0 END) AS num_recent_queries,
            roles.active,
            roles.role = "instructor" AS instructor_role
        FROM users
        LEFT JOIN auth_providers ON users.auth_provider=auth_providers.id
        JOIN roles ON roles.user_id=users.id
        LEFT JOIN queries ON queries.role_id=roles.id
        WHERE roles.class_id=?
        GROUP BY users.id
        ORDER BY display_name
    """, [class_id]).fetchall()

    return users


@bp.route("/")
@instructor_required
def main() -> str | Response:
    auth = get_auth()
    class_id = auth['class_id']
    assert class_id is not None

    users = get_users(class_id)

    sel_user_name = None
    sel_user_id = request.args.get('user', type=int)
    if sel_user_id is not None:
        sel_user_row = next(filter(lambda row: row['id'] == int(sel_user_id), users), None)
        if sel_user_row:
            sel_user_name = sel_user_row['display_name']

    queries = get_queries(class_id, sel_user_id)

    return render_template("instructor.html", users=users, queries=queries, user=sel_user_name)


@bp.route("/csv/<string:kind>")
@instructor_required
def get_csv(kind: str) -> str | Response:
    if kind not in ('queries', 'users'):
        return abort(404)

    auth = get_auth()
    class_id = auth['class_id']
    class_name = auth['class_name']
    assert class_id is not None
    assert class_name is not None

    if kind == "queries":
        table = get_queries(class_id)
    elif kind == "users":
        table = get_users(class_id, for_export=True)

    return csv_response(class_name, kind, table)


@bp.route("/user_class/set", methods=["POST"])
@instructor_required
def set_user_class_setting() -> Response:
    db = get_db()
    auth = get_auth()

    # only trust class_id from auth, not from user
    class_id = auth['class_id']

    if 'clear_openai_key' in request.form:
        db.execute("UPDATE classes_user SET openai_key='' WHERE class_id=?", [class_id])
        db.commit()
        flash("Class API key cleared.", "success")

    elif 'save_access_form' in request.form:
        if 'link_reg_active_present' in request.form:
            # only present for user classes, not LTI
            link_reg_active = request.form['link_reg_active']
            if link_reg_active == "disabled":
                new_date = str(dt.date.min)
            elif link_reg_active == "enabled":
                new_date = str(dt.date.max)
            else:
                new_date = request.form['link_reg_expires']
            db.execute("UPDATE classes_user SET link_reg_expires=? WHERE class_id=?", [new_date, class_id])
        class_enabled = 1 if 'class_enabled' in request.form else 0
        db.execute("UPDATE classes SET enabled=? WHERE id=?", [class_enabled, class_id])
        db.commit()
        flash("Class access configuration updated.", "success")

    elif 'save_llm_form' in request.form:
        if 'openai_key' in request.form:
            db.execute("UPDATE classes_user SET openai_key=? WHERE class_id=?", [request.form['openai_key'], class_id])
        db.execute("UPDATE classes_user SET model_id=? WHERE class_id=?", [request.form['model_id'], class_id])
        db.commit()
        flash("Class language model configuration updated.", "success")

    return redirect(request.referrer)


@bp.route("/role/set_active", methods=["POST"])  # just for url_for in the Javascript code
@bp.route("/role/set_active/<int:role_id>/<int:bool_active>", methods=["POST"])
@instructor_required
def set_role_active(role_id: int, bool_active: int) -> str:
    db = get_db()
    auth = get_auth()

    # prevent instructors from mistakenly making themselves not active and locking themselves out
    if role_id == auth['role_id']:
        return "You cannot make yourself inactive."

    # class_id should be redundant w/ role_id, but without it, an instructor
    # could potentially deactivate a role in someone else's class.
    # only trust class_id from auth, not from user
    class_id = auth['class_id']

    db.execute("UPDATE roles SET active=? WHERE id=? AND class_id=?", [bool_active, role_id, class_id])
    db.commit()

    return "okay"


@bp.route("/role/set_instructor", methods=["POST"])  # just for url_for in the Javascript code
@bp.route("/role/set_instructor/<int:role_id>/<int:bool_instructor>", methods=["POST"])
@instructor_required
def set_role_instructor(role_id: int, bool_instructor: int) -> str:
    db = get_db()
    auth = get_auth()

    # prevent instructors from mistakenly making themselves not instructors and locking themselves out
    if role_id == auth['role_id']:
        return "You cannot change your own role."

    # class_id should be redundant w/ role_id, but without it, an instructor
    # could potentially deactivate a role in someone else's class.
    # only trust class_id from auth, not from user
    class_id = auth['class_id']

    new_role = 'instructor' if bool_instructor else 'student'

    db.execute("UPDATE roles SET role=? WHERE id=? AND class_id=?", [new_role, role_id, class_id])
    db.commit()

    return "okay"

@bp.route("/contexts")
def contexts() -> str:
    db = get_db()
    contexts = db.execute("SELECT * FROM contexts").fetchall()
    return render_template("contexts.html", contexts=contexts)

@bp.route("/contexts/new")
def context_new() -> str:
    return render_template("context_form.html")

@bp.route("/contexts/<int:id>")
def context_form(id: int | None = None) -> str:
    db = get_db()
    context_row = db.execute("SELECT * FROM contexts WHERE id=?", [id]).fetchone()
    return render_template("context_form.html", contexts=context_row)

@bp.route("/contexts/save", methods=["POST"])
def context_save() -> Response:
    db = get_db()
    context_id = request.form.get('context_id', type=int)
    display_name = get_auth()['display_name']
    if context_id is None:
        cur = db.execute("INSERT INTO contexts (context_name, description, avoid_set, user_id) VALUES (?, ?, ?, ?)", [request.form['context_name'], request.form['description'], request.form['avoid_set'], display_name])
        context_id = cur.lastrowid
    else:
        db.execute("UPDATE contexts SET context_name=?, description=?, avoid_set=? WHERE id=?", [request.form['context_name'], request.form['description'], request.form['avoid_set'], context_id])
    db.commit()
    return redirect("/instructor/contexts")

@bp.route("/contexts/delete/<int:id>")
def context_delete(id: int) -> Response:
    db = get_db()
    db.execute("DELETE FROM contexts WHERE id=?", [id])
    db.commit()
    return redirect("/instructor/contexts")