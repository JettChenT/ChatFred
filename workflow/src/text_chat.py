"""This module contains the ChatGPT API."""

import csv
import functools
import os
import sys
import time
import uuid
from typing import Dict, List, Optional, Tuple

from aliases_manager import prompt_for_alias
from caching_manager import read_from_cache, write_to_cache
from custom_prompts import clear_log_prompts, error_prompts
from error_handler import (
    env_value_error_if_needed,
    exception_response,
    get_last_error_message,
    log_error_if_needed,
)

sys.path.append(os.path.join(os.path.dirname(__file__), "libs"))

import openai

openai.api_key = os.getenv("api_key")

__model = os.getenv("chat_gpt_model") or "gpt-3.5-turbo"
__history_length = int(os.getenv("history_length") or 4)
__temperature = float(os.getenv("temperature") or 0.0)
__max_tokens = int(os.getenv("chat_max_tokens")) if os.getenv("chat_max_tokens") else None  # type: ignore
__top_p = int(os.getenv("top_p") or 1)
__frequency_penalty = float(os.getenv("frequency_penalty") or 0.0)
__presence_penalty = float(os.getenv("presence_penalty") or 0.0)
__workflow_data_path = os.getenv("alfred_workflow_data") or os.path.expanduser("~")
__log_file_path = f"{__workflow_data_path}/ChatFred_ChatGPT.csv"
__text_transformation_prompt = os.getenv("text_transformation_prompt") or None
__jailbreak_prompt = os.getenv("jailbreak_prompt")
__unlocked = int(os.getenv("unlocked") or 0)


def time_it(func):
    """A decorator function that times the execution of a given function.

    Args:
        func: The function to be timed.

    Returns:
        A wrapper function that times the execution of the input function.
    """

    @functools.wraps(func)
    def timeit_wrapper(*args):
        start_time = time.perf_counter()
        result = func(*args)
        end_time = time.perf_counter()
        total_time = end_time - start_time
        print(
            f"Function: {func.__name__} took {total_time:.4f} seconds\n",
            file=sys.stderr,
        )
        return result

    return timeit_wrapper


def get_query() -> str:
    """Returns a string of all command line arguments passed to the script.

    Returns:
        str: A string of all command line arguments passed to the script.
    """
    return " ".join(sys.argv[1:])


def stdout_write(output_string: str) -> None:
    """Writes the given string to the standard output.

    Args:
        output_string (str): The string to be written to the standard output.

    Returns:
        None
    """
    output_string = "..." if output_string == "" else output_string
    sys.stdout.write(output_string)


def exit_on_error() -> None:
    """Exits the program and shows some message if there is an error.

    Args:
        None

    Returns:
        None
    """
    error = env_value_error_if_needed(
        __temperature,
        __model,
        __max_tokens,
        __frequency_penalty,
        __presence_penalty,
    )
    if error:
        stdout_write(error)
        sys.exit(0)


@time_it
def read_from_log() -> List[Tuple[str, str]]:
    """Reads the last __history_length entries from the log file.

    Returns:
        List[Tuple[str, str]]: A list of tuples containing the last __history_length entries from the log file.
            Each tuple contains two strings: the first is the timestamp and the second is the log message.
            If the log file does not exist, returns a list with one empty tuple.
    """
    if os.path.isfile(__log_file_path) is False:
        return [("", "")]

    with open(__log_file_path, "r") as csv_file:
        csv.register_dialect("custom", delimiter=" ", skipinitialspace=True)
        reader = csv.reader(csv_file, dialect="custom")

        history = []
        for row in reader:
            history.append((row[1], row[2]))

    return history[-__history_length:]


@time_it
def write_to_log(
    user_input: str, assistant_output: str, jailbreak_prompt: Optional[str] = None
) -> None:
    """Writes user input and assistant output to a log file.

    Args:
        user_input (str): The user input to be logged.
        assistant_output (str): The assistant output to be logged.
        jailbreak_prompt (Optional[str]): A prompt to be logged if the user attempts to jailbreak the system. Defaults to None.

    Returns:
        None
    """
    if not os.path.exists(__workflow_data_path):
        os.makedirs(__workflow_data_path)
    with open(__log_file_path, "a+") as csv_file:
        csv.register_dialect("custom", delimiter=" ", skipinitialspace=True)
        writer = csv.writer(csv_file, dialect="custom")
        if jailbreak_prompt:
            writer.writerow(
                [str(uuid.uuid1()), jailbreak_prompt, "Okay! How can I help?", 1]
            )
        writer.writerow([str(uuid.uuid1()), user_input, assistant_output, 0])


def remove_log_file() -> None:
    """Removes the log file."""
    if os.path.isfile(__log_file_path):
        os.remove(__log_file_path)


def intercept_custom_prompts(prompt: str) -> None:
    """Intercepts custom queries.

    Args:
        prompt (str): The prompt to intercept.

    Returns:
        None
    """
    last_request_successful = read_from_cache("last_chat_request_successful")
    if prompt in error_prompts and not last_request_successful:
        stdout_write(
            f"😬 Sorry, the error message was not really helpful. Here is the original message from OpenAI:\n\n➡️ {get_last_error_message()}"
        )
        write_to_cache("last_chat_request_successful", True)
        sys.exit(0)

    if prompt in clear_log_prompts:
        remove_log_file()
        stdout_write("All my memories of you have been erased 😢")
        sys.exit(0)


@time_it
def create_message(prompt: str) -> List[Dict[str, str]]:
    """Creates a message to be sent to the model.

    Args:
        prompt (str): The prompt to be included in the message.

    Returns:
        List[Dict[str, str]]: A list of dictionaries representing the message,
            with each dictionary containing the role and content of the message.
    """
    transformation_pre_prompt = """You are a helpful assistant who interprets every input as raw
    text unless instructed otherwise. Your answers do not include a description unless prompted to do so.
    Also drop any "`" characters from the your response."""

    if __text_transformation_prompt:
        return [
            {"role": "system", "content": transformation_pre_prompt},
            {
                "role": "user",
                "content": f"{__text_transformation_prompt} Don't add any comments: {prompt}",
            },
        ]

    messages = [{"role": "system", "content": "You are a helpful assistant"}]
    for user_text, assistant_text in read_from_log():
        if user_text == __jailbreak_prompt:
            continue
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": assistant_text})

    if __jailbreak_prompt and __unlocked == 1:
        messages.append({"role": "user", "content": __jailbreak_prompt})
        messages.append({"role": "assistant", "content": "Okay! How can I help?"})

    messages.append({"role": "user", "content": prompt})
    return messages


@time_it
def make_chat_request(
    prompt: str,
    temperature: float,
    max_tokens: Optional[int],
    top_p: int,
    frequency_penalty: float,
    presence_penalty: float,
) -> Tuple[str, str]:
    """Sends a chat request to OpenAI's GTP-3 model and returns the prompt and
    response as a tuple.

    Args:
        prompt (str): The prompt to send to the model.
        temperature (float): Controls the "creativity" of the response. Higher values result in more diverse responses.
        max_tokens (Optional[int]): The maximum number of tokens (words) in the response.
        top_p (int): Controls the "quality" of the response. Higher values result in more coherent responses.
        frequency_penalty (float): Controls the model's tendency to repeat itself.
        presence_penalty (float): Controls the model's tendency to stay on topic.

    Returns:
        Tuple[str, str]: A tuple containing the prompt and the response from the model.
    """
    intercept_custom_prompts(prompt)
    prompt = prompt_for_alias(prompt)
    messages = create_message(prompt)
    write_to_cache("last_chat_request_successful", True)

    try:
        response = (
            openai.ChatCompletion.create(
                model=__model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
            )
            .choices[0]
            .message["content"]
        )

    except Exception as exception:  # pylint: disable=broad-except
        response = exception_response(exception)
        write_to_cache("last_chat_request_successful", False)
        log_error_if_needed(
            model=__model,
            error_message=exception._message,  # type: ignore  # pylint: disable=protected-access
            user_prompt=prompt,
            parameters={
                "temperature": temperature,
                "max_tokens": max_tokens,
                "top_p": top_p,
                "frequency_penalty": frequency_penalty,
                "presence_penalty": presence_penalty,
            },
        )

    return prompt, response


exit_on_error()
__prompt, __response = make_chat_request(
    get_query(),
    __temperature,
    __max_tokens,
    __top_p,
    __frequency_penalty,
    __presence_penalty,
)
stdout_write(__response)
write_to_log(__prompt, __response, __jailbreak_prompt if __unlocked else None)
