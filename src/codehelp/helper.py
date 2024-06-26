# SPDX-FileCopyrightText: 2023 Mark Liffiton <liffiton@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-only

import asyncio
import json
from collections.abc import Iterable
from unittest.mock import patch

from flask import Blueprint, abort, redirect, render_template, request, url_for
from gened.auth import admin_required, class_enabled_required, get_auth, login_required, tester_required
from gened.db import get_db
from gened.openai import LLMDict, mock_async_completion, get_completion, with_llm
from gened.queries import get_history, get_query
from openai.types.chat.chat_completion import ChatCompletion
from werkzeug.wrappers.response import Response

from . import prompts
from .class_config import get_class_config

bp = Blueprint('helper', __name__, url_prefix="/help", template_folder='templates')


@bp.route("/")
@bp.route("/<int:query_id>")
@login_required
@class_enabled_required
def help_form(query_id: int | None = None) -> str:
    db = get_db()
    auth = get_auth()
    class_config = get_class_config()

    languages = class_config.languages
    contexts = db.execute("SELECT * FROM contexts").fetchall()
    contexts = [context['context_name'] for context in contexts]
    selected_lang = class_config.default_lang

    # Select most recently submitted language, if available
    lang_row = db.execute("SELECT language FROM queries WHERE queries.user_id=? ORDER BY query_time DESC LIMIT 1", [auth['user_id']]).fetchone()
    if lang_row and lang_row['language'] in languages:
        selected_lang = lang_row['language']

    # populate with a query+response if one is specified in the query string
    query_row = None
    if query_id is not None:
        query_row, _ = get_query(query_id)   # _ because we don't need responses here
        if query_row is not None:
            selected_lang = query_row['language']

    history = get_history()

    return render_template("help_form.html", query=query_row, history=history, languages=languages, selected_lang=selected_lang, contexts=contexts)


@bp.route("/view/<int:query_id>")
@login_required
def help_view(query_id: int) -> str:
    query_row, responses = get_query(query_id)
    history = get_history()
    if query_row and query_row['topics_json']:
        topics = json.loads(query_row['topics_json'])
    else:
        topics = []

    return render_template("help_view.html", query=query_row, responses=responses, history=history, topics=topics)


def score_response(response_txt: str | None, avoid_set: Iterable[str]) -> int:
    ''' Return an integer score for a given response text.
    Returns:
        0 = best.
        Negative values for responses including indications of code blocks or keywords in the avoid set.
        Indications of code blocks are weighted most heavily.
    '''
    if not response_txt:
        # Empty/None response is pretty bad.
        return -100

    score = 0
    for bad_kw in avoid_set:
        score -= response_txt.count(bad_kw)
    for code_indication in ['```', 'should look like', 'should look something like']:
        score -= 100 * response_txt.count(code_indication)

    return score


async def run_query_prompts(llm_dict: LLMDict, language: str, code: str, error: str, issue: str, context: str, avoid_set: list[str]) -> tuple[list[dict[str, str]], dict[str, str]]:
    ''' Run the given query against the coding help system of prompts.

    Returns a tuple containing:
      1) A list of response objects from the OpenAI completion (to be stored in the database)
      2) A dictionary of response text, potentially including keys 'insufficient' and 'main'.
    '''
    client = llm_dict['client']
    model = llm_dict['model']

    # create "avoid set" from class configuration
    class_config = get_class_config()

    # Launch the "sufficient detail" check concurrently with the main prompt to save time
    task_main = asyncio.create_task(
        get_completion(
            client,
            model=model,
            n=1,
            prompt=prompts.make_main_prompt(language, code, error, issue, avoid_set, context),
            score_func=lambda x: score_response(x, avoid_set)
        )
    )
    task_sufficient = asyncio.create_task(
        get_completion(
            client,
            model=model,
            prompt=prompts.make_sufficient_prompt(language, code, error, issue, context),
        )
    )

    # Store all responses received
    responses = []

    # Let's get the main response.
    response_main, response_txt = await task_main
    responses.append(response_main)

    if "```" in response_txt or "should look like" in response_txt or "should look something like" in response_txt:
        # That's probably too much code.  Let's clean it up...
        cleanup_prompt = prompts.make_cleanup_prompt(response_text=response_txt)
        cleanup_response, cleanup_response_txt = await get_completion(client, model, prompt=cleanup_prompt)
        responses.append(cleanup_response)
        response_txt = cleanup_response_txt

    # Check whether there is sufficient information
    # Checking after processing main+cleanup prevents this from holding up the start of cleanup if this was slow
    response_sufficient, response_sufficient_txt = await task_sufficient
    responses.append(response_sufficient)

    if 'error' in response_main:
        return responses, {'error': response_txt}
    elif response_sufficient_txt.endswith("OK") or "OK." in response_sufficient_txt or "```" in response_sufficient_txt or "is sufficient for me" in response_sufficient_txt or response_sufficient_txt.startswith("Error ("):
        # We're using just the main response.
        return responses, {'main': response_txt}
    else:
        # Give them the request for more information plus the main response, in case it's helpful.
        return responses, {'insufficient': response_sufficient_txt, 'main': response_txt}


def run_query(llm_dict: LLMDict, language: str, code: str, error: str, issue: str, context_id: str, context: str, avoid_set: str) -> int:

    query_id = record_query(language, code, error, issue, context_id)
    responses, texts = asyncio.run(run_query_prompts(llm_dict, language, code, error, issue, context, avoid_set))

    record_response(query_id, responses, texts)

    return query_id


def record_query(language: str, code: str, error: str, issue: str, context: str) -> int:
    db = get_db()
    auth = get_auth()
    role_id = auth['role_id']

    cur = db.execute(
        "INSERT INTO queries (language, code, error, issue, user_id, role_id, context) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [language, code, error, issue, auth['user_id'], role_id, context]
    )
    new_row_id = cur.lastrowid
    db.commit()

    assert new_row_id is not None

    return new_row_id


def record_response(query_id: int, responses: list[dict[str, str]], texts: dict[str, str]) -> None:
    db = get_db()

    db.execute(
        "UPDATE queries SET response_json=?, response_text=? WHERE id=?",
        [json.dumps(responses), json.dumps(texts), query_id]
    )
    db.commit()


@bp.route("/request", methods=["POST"])
@login_required
@class_enabled_required
@with_llm()
def help_request(llm_dict: LLMDict) -> Response:
    class_config = get_class_config()
    if class_config.languages:
        lang_id = int(request.form["lang_id"])
        language = class_config.languages[lang_id]
    else:
        language = ""
    code = request.form["code"]
    error = request.form["error"]
    issue = request.form["issue"]
    db = get_db()
    context_row = db.execute("SELECT * FROM contexts WHERE context_name=?", [request.form["context_id"]]).fetchone()
    context = context_row['description'] if context_row else ""
    avoid_set = {x.strip() for x in context_row['avoid_set'].split('\n') if x.strip() != ''}

    # TODO: limit length of code/error/issue
    query_id = run_query(llm_dict, language, code, error, issue, request.form["context_id"], context, avoid_set)

    return redirect(url_for(".help_view", query_id=query_id))


@bp.route("/load_test", methods=["POST"])
@admin_required
@with_llm(use_system_key=True)  # get a populated LLMDict
def load_test(llm_dict: LLMDict) -> Response:
    # Require that we're logged in as the load_test admin user
    auth = get_auth()
    if auth['display_name'] != 'load_test':
        return abort(403)

    language = "__LOADTEST_Language"
    code = "__LOADTEST_Code"
    error = "__LOADTEST_Error"
    issue = "__LOADTEST_Issue"
    context = "__LOADTEST_Context"
    context_id = "__LOADTEST_ContextID"

    avoid_set = []

    # Monkey-patch to not call the API but simulate it with a delay
    with patch("openai.resources.chat.AsyncCompletions.create") as mocked:
        # simulate a 2 second delay for a network request
        mocked.side_effect = mock_async_completion(delay=2.0)

        query_id = run_query(llm_dict, language, code, error, issue, context_id, context, avoid_set)

    return redirect(url_for(".help_view", query_id=query_id))


@bp.route("/post_helpful", methods=["POST"])
@login_required
def post_helpful() -> str:
    db = get_db()
    auth = get_auth()

    query_id = int(request.form['id'])
    value = int(request.form['value'])
    db.execute("UPDATE queries SET helpful=? WHERE id=? AND user_id=?", [value, query_id, auth['user_id']])
    db.commit()
    return ""


@bp.route("/topics/html/<int:query_id>", methods=["GET", "POST"])
@login_required
@tester_required
@with_llm()
def get_topics_html(llm_dict: LLMDict, query_id: int) -> str:
    topics = get_topics(llm_dict, query_id)
    if not topics:
        return render_template("topics_fragment.html", error=True)
    else:
        return render_template("topics_fragment.html", query_id=query_id, topics=topics)


@bp.route("/topics/raw/<int:query_id>", methods=["GET", "POST"])
@login_required
@tester_required
@with_llm()
def get_topics_raw(llm_dict: LLMDict, query_id: int) -> list[str]:
    topics = get_topics(llm_dict, query_id)
    return topics


def get_topics(llm_dict: LLMDict, query_id: int) -> list[str]:
    query_row, responses = get_query(query_id)

    if not query_row or not responses or 'main' not in responses:
        return []

    messages = prompts.make_topics_prompt(
        query_row['language'],
        query_row['code'],
        query_row['error'],
        query_row['issue'],
        responses['main']
    )

    response, response_txt = asyncio.run(get_completion(
        client=llm_dict['client'],
        model=llm_dict['model'],
        messages=messages,
    ))

    # Verify it is actually JSON
    # May be "Error (..." if an API error occurs, or every now and then may get "Here is the JSON: ..." or similar.
    try:
        topics: list[str] = json.loads(response_txt)
    except json.decoder.JSONDecodeError:
        return []

    # Save topics into queries table for the given query
    db = get_db()
    db.execute("UPDATE queries SET topics_json=? WHERE id=?", [response_txt, query_id])
    db.commit()
    return topics
