#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at https://mozilla.org/MPL/2.0/.
import enum
import os
import typing
from datetime import datetime

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ValidationError
from classquiz.config import redis
from classquiz.db.models import PlayGame, QuizQuestionType, AnswerData, GamePlayer
from classquiz.socket_server import sio, calculate_score, set_answer

router = APIRouter()


class JoinGameInput(BaseModel):
    code: str
    name: str


class JoinGameResponse(BaseModel):
    id: str


@router.post("/join")
async def join_game(data: JoinGameInput):
    game_pin = await redis.get(f"game:cqc:code:{data.code}")
    if game_pin is None:
        raise HTTPException(status_code=404, detail="Game not found")
    game = await redis.get(f"game:{game_pin}")
    game = PlayGame.parse_raw(game)
    # Check if game is already running
    if game.started:
        raise HTTPException(status_code=400, detail="Game started already")
    # check if username already exists
    if await redis.get(f"game_session:{game_pin}:players:{data.name}") is not None:
        raise HTTPException(status_code=409, detail="Username already exists")

    player_id = os.urandom(5).hex()
    await redis.set(f"game:cqc:player:{player_id}", data.name)
    return JoinGameResponse(id=f"{player_id}:{game_pin}")


class SubmitAnswerInput(BaseModel):
    answer: int


async def submit_answer_fn(data_answer: int, game_pin: str, player_id: str, now: datetime):
    redis_res_game = await redis.get(f"game:{game_pin}")
    username = await redis.get(f"game:cqc:player:{player_id}")
    if redis_res_game is None or username is None:
        raise HTTPException(status_code=404, detail="id not existent")
    game = PlayGame.parse_raw(redis_res_game)
    if not game.question_show:
        return
    question = game.questions[game.current_question]
    question_time = datetime.fromisoformat(await redis.get(f"game:{game_pin}:current_time"))
    try:
        selected_answer = question.answers[data_answer].answer
    except KeyError:
        return
    answer_right = False
    if question.type == QuizQuestionType.ABCD:
        for answer in question.answers:
            if answer.answer == selected_answer and answer.right:
                answer_right = True
                break
    elif question.type == QuizQuestionType.VOTING:
        answer_right = False
    else:
        return
    diff = (question_time - now).total_seconds() * 1000
    score = 0
    if answer_right:
        score = calculate_score(abs(diff), int(float(question.time)))
    await redis.hincrby(f"game_session:{game_pin}:player_scores", username, score)
    answer_data = AnswerData(
        username=username,
        answer=selected_answer,
        right=answer_right,
        time_taken=abs(diff),
        score=score,
    )
    answers = await redis.get(f"game_session:{game_pin}:{game.current_question}")
    answers = await set_answer(answers, game_pin=game_pin, data=answer_data, q_index=game.current_question)
    player_count = await redis.scard(f"game_session:{game_pin}:players")
    print(player_count, answers)
    if answers is not None and len(answers.__root__) == player_count:
        await sio.emit("everyone_answered", {})


class WebSocketTypes(enum.Enum):
    ButtonPress = "bp"
    Error = "e"


class WebSocketRequest(BaseModel):
    type: WebSocketTypes
    data: typing.Any


wss_clients = {}

button_to_index_map = {"b": 0, "g": 1, "y": 2, "r": 3}


@router.websocket("/socket/{id}")
async def websocket_endpoint(ws: WebSocket, game_id: str, id: str):
    try:
        if id in wss_clients.keys():
            await ws.close(code=status.WS_1001_GOING_AWAY)
            print("Client {} already exists.".format(id))
            return

        await ws.accept()
        wss_clients[id] = ws

        player_id, game_pin = game_id.split(":")
        if player_id is None or game_pin is None:
            await ws.send_text(WebSocketRequest(type=WebSocketTypes.Error, data="BadId").json())
            await ws.close(code=status.WS_1003_UNSUPPORTED_DATA)
        username = await redis.get(f"game:cqc:player:{player_id}")
        await sio.emit(
            "player_joined",
            {"username": username, "sid": None},
            room=f"admin:{game_pin}",
        )
        await redis.sadd(f"game_session:{game_pin}:players", GamePlayer(username=username, sid=None).json())

        while True:
            raw_data = await ws.receive_text()
            try:
                data = WebSocketRequest.parse_raw(raw_data)
            except ValidationError:
                await ws.send_text(WebSocketRequest(type=WebSocketTypes.Error, data="ValidationError").json())
                continue

            if data.type == WebSocketTypes.ButtonPress:
                now = datetime.now()
                try:
                    answer_index = button_to_index_map[data.data.lower()]
                except (KeyError, AttributeError):
                    await ws.send_text(WebSocketRequest(type=WebSocketTypes.Error, data="InvalidButton").json())
                    continue

                await submit_answer_fn(answer_index, game_pin, player_id, now)
            print("Data from client {}: {}".format(id, data))

    except WebSocketDisconnect as ex:
        print("Client {} is disconnected: {}".format(id, ex))
        wss_clients.pop(id, None)
