from flask import current_app
import openai

from .db import get_db
from .auth import get_session_auth


def get_openai_key():
    '''Get the openai API key, using the key stored for the current consumer
       or else use the default key in the config for non-LTI users.
    '''
    db = get_db()
    auth = get_session_auth()
    if auth['class_id'] is None:
        # default key for non-LTI users
        key = current_app.config["OPENAI_API_KEY"]
    else:
        consumer_row = db.execute("""
            SELECT consumers.openai_key
            FROM classes_lti
            JOIN consumers
              ON classes_lti.lti_consumer_id = consumers.id
            WHERE classes_lti.class_id=?
        """, [auth['class_id']]).fetchone()
        key = consumer_row['openai_key']

    return key


async def get_completion(api_key, prompt=None, messages=None, model='turbo', n=1, score_func=None):
    '''
    model can be either 'davinci' or 'turbo'

    Returns:
       - A tuple containing:
           - An OpenAI response object
           - The response text (stripped)
    '''
    assert prompt is None or messages is None
    if model == 'davinci':
        assert prompt is not None

    try:
        if model == 'davinci':
            response = await openai.Completion.acreate(
                api_key=api_key,
                model="text-davinci-003",
                prompt=prompt,
                temperature=0.25,
                max_tokens=1000,
                n=n,
                # TODO: add user= parameter w/ unique ID of user (e.g., hash of username+email or similar)
            )
            get_text = lambda choice: choice.text  # noqa
        elif model == 'turbo':
            if messages is None:
                messages = [{"role": "user", "content": prompt}]
            response = await openai.ChatCompletion.acreate(
                api_key=api_key,
                model="gpt-3.5-turbo",
                messages=messages,
                temperature=0.25,
                max_tokens=1000,
                n=n,
                # TODO: add user= parameter w/ unique ID of user (e.g., hash of username+email or similar)
            )
            get_text = lambda choice: choice.message['content']  # noqa

        if n > 1:
            best_choice = max(response.choices, key=lambda choice: score_func(get_text(choice)))
        else:
            best_choice = response.choices[0]
        response_txt = get_text(best_choice)

        response_reason = best_choice.finish_reason  # e.g. "length" if max_tokens reached

        if response_reason == "length":
            response_txt += "\n\n[error: maximum length exceeded]"

    except openai.error.APIError as e:
        current_app.logger.error(e)
        response = str(e)
        response_txt = "Error (APIError).  Something went wrong on our side.  Please try again, and if it repeats, let us know using the contact form at the bottom of the page."
        pass
    except openai.error.Timeout as e:
        current_app.logger.error(e)
        response = str(e)
        response_txt = "Error (Timeout).  Something went wrong on our side.  Please try again, and if it repeats, let us know using the contact form at the bottom of the page."
        pass
    except openai.error.ServiceUnavailableError as e:
        current_app.logger.error(e)
        response = str(e)
        response_txt = "Error (ServiceUnavailableError).  Something went wrong on our side.  Please try again, and if it repeats, let us know using the contact form at the bottom of the page."
        pass
    except openai.error.RateLimitError as e:
        current_app.logger.error(e)
        response = str(e)
        response_txt = "Error (RateLimitError).  The system is receiving too many requests right now.  Please try again in one minute.  If it does not resolve, please let us know using the contact form at the bottom of the page."
        pass
    except Exception as e:
        current_app.logger.error(e)
        response = str(e)
        response_txt = "Error (Exception).  Something went wrong on our side.  Please try again, and if it repeats, let us know using the contact form at the bottom of the page."
        pass

    return response, response_txt.strip()
