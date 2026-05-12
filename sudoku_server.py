#!/usr/bin/env python3
"""Sudoku WebSocket server - handles puzzle generation, validation, and hints."""

import asyncio
import json
import random
import websockets
from concurrent.futures import ThreadPoolExecutor

# Thread pool for running blocking solver calls without freezing the event loop
_executor = ThreadPoolExecutor(max_workers=4)

SOLVER_TIMEOUT = 5.0   # seconds before a solve attempt is aborted

# ── Sudoku Engine ──────────────────────────────────────────────────────────────

def is_valid(board, row, col, num):
    """Check if placing num at (row, col) is valid."""
    if num in board[row]:
        return False
    if num in [board[r][col] for r in range(9)]:
        return False
    br, bc = 3 * (row // 3), 3 * (col // 3)
    for r in range(br, br + 3):
        for c in range(bc, bc + 3):
            if board[r][c] == num:
                return False
    return True


def _solve(board):
    """Backtracking solver. Returns True if solved, mutates board in-place."""
    for row in range(9):
        for col in range(9):
            if board[row][col] == 0:
                nums = list(range(1, 10))
                random.shuffle(nums)
                for num in nums:
                    if is_valid(board, row, col, num):
                        board[row][col] = num
                        if _solve(board):
                            return True
                        board[row][col] = 0
                return False
    return True


def _count_solutions(board, limit=2):
    """Count solutions up to limit (synchronous, for use in executor)."""
    count = [0]

    def recurse(b):
        if count[0] >= limit:
            return
        for row in range(9):
            for col in range(9):
                if b[row][col] == 0:
                    for num in range(1, 10):
                        if is_valid(b, row, col, num):
                            b[row][col] = num
                            recurse(b)
                            b[row][col] = 0
                    return
        count[0] += 1

    recurse([row[:] for row in board])
    return count[0]


def _generate_puzzle_sync(difficulty):
    """Synchronous puzzle generation (runs in executor)."""
    clue_counts = {"easy": 38, "medium": 30, "hard": 24}
    target_clues = clue_counts.get(difficulty, 30)

    board = [[0] * 9 for _ in range(9)]
    _solve(board)
    solution = [row[:] for row in board]
    puzzle = [row[:] for row in board]

    cells = [(r, c) for r in range(9) for c in range(9)]
    random.shuffle(cells)
    removed = 0

    for row, col in cells:
        if removed >= 81 - target_clues:
            break
        backup = puzzle[row][col]
        puzzle[row][col] = 0
        if _count_solutions(puzzle) == 1:
            removed += 1
        else:
            puzzle[row][col] = backup

    return puzzle, solution


def _validate_board_sync(puzzle, current):
    """Synchronous board validation (runs in executor)."""
    solution_board = [row[:] for row in puzzle]
    _solve(solution_board)
    result = []
    for r in range(9):
        row_result = []
        for c in range(9):
            if puzzle[r][c] != 0:
                row_result.append("fixed")
            elif current[r][c] == 0:
                row_result.append("empty")
            elif current[r][c] == solution_board[r][c]:
                row_result.append("correct")
            else:
                row_result.append("wrong")
        result.append(row_result)
    return result, solution_board


def is_complete(current, solution):
    return all(current[r][c] == solution[r][c] for r in range(9) for c in range(9))


# ── Async wrappers with timeout ────────────────────────────────────────────────

async def generate_puzzle_async(difficulty):
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_executor, _generate_puzzle_sync, difficulty),
        timeout=SOLVER_TIMEOUT * 3,
    )


async def validate_board_async(puzzle, current):
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_executor, _validate_board_sync, puzzle, current),
        timeout=SOLVER_TIMEOUT,
    )


async def solve_board_async(board):
    """Solve board in-place in executor. Returns True if solved."""
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_executor, _solve, board),
        timeout=SOLVER_TIMEOUT,
    )


# ── Input validation ───────────────────────────────────────────────────────────

class ValidationError(Exception):
    pass


def _require_int(value, name, lo, hi):
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    if not (lo <= value <= hi):
        raise ValidationError(f"{name} must be between {lo} and {hi}")
    return value


def _require_81_digit_string(value, name, allow_zero=True):
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    if len(value) != 81:
        raise ValidationError(f"{name} must be exactly 81 characters")
    for ch in value:
        if not ch.isdigit():
            raise ValidationError(f"{name} contains non-digit character: {ch!r}")
        if not allow_zero and ch == '0':
            raise ValidationError(f"{name} must not contain zeros (solution must be complete)")
    return value


def _validate_puzzle_consistency(raw_puzzle, raw_current):
    """Fixed clues in puzzle must not be overwritten in current."""
    for i in range(81):
        p, c = int(raw_puzzle[i]), int(raw_current[i])
        if p != 0 and c != p:
            raise ValidationError("current board overwrites a fixed clue")


def _validate_no_conflicts(raw, name):
    """No digit may appear twice in any row, column, or 3x3 box."""
    board = [[int(raw[r*9+c]) for c in range(9)] for r in range(9)]
    for r in range(9):
        digits = [v for v in board[r] if v != 0]
        if len(digits) != len(set(digits)):
            raise ValidationError(f"{name} has a row conflict")
    for c in range(9):
        digits = [board[r][c] for r in range(9) if board[r][c] != 0]
        if len(digits) != len(set(digits)):
            raise ValidationError(f"{name} has a column conflict")
    for br in range(0, 9, 3):
        for bc in range(0, 9, 3):
            digits = [board[br+dr][bc+dc]
                      for dr in range(3) for dc in range(3)
                      if board[br+dr][bc+dc] != 0]
            if len(digits) != len(set(digits)):
                raise ValidationError(f"{name} has a box conflict")


def _validate_notes_flat(tokens):
    if not isinstance(tokens, list):
        raise ValidationError("notes_flat must be a list")
    if len(tokens) != 81:
        raise ValidationError("notes_flat must have exactly 81 entries")
    for i, tok in enumerate(tokens):
        if not isinstance(tok, str):
            raise ValidationError(f"notes_flat[{i}] must be a string")
        if tok not in ('.', '-') and not all(c in '123456789' for c in tok):
            raise ValidationError(f"notes_flat[{i}] has invalid value: {tok!r}")
    return tokens


def _validate_difficulty(value):
    if value not in ("easy", "medium", "hard"):
        raise ValidationError("difficulty must be easy, medium, or hard")
    return value


def from_line(s):
    return [[int(s[r*9+c]) for c in range(9)] for r in range(9)]


def to_line(board):
    return "".join(str(board[r][c]) for r in range(9) for c in range(9))


# ── WebSocket Handler ──────────────────────────────────────────────────────────

async def handler(websocket):
    """Handle a single client connection."""
    game_state = {
        "puzzle":     None,
        "solution":   None,
        "current":    None,
        "difficulty": "medium",
        "hint_count": 0,
        "mistakes":   0,
    }

    async def send(msg: dict):
        await websocket.send(json.dumps(msg))

    async def send_error(message: str):
        await send({"type": "error", "message": message})

    async for raw in websocket:
        # ── Parse ──────────────────────────────────────────────────────────────
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await send_error("Invalid JSON")
            continue

        if not isinstance(msg, dict):
            await send_error("Message must be a JSON object")
            continue

        action = msg.get("type")
        if not isinstance(action, str):
            await send_error("Missing or invalid 'type' field")
            continue

        # ── Dispatch ───────────────────────────────────────────────────────────
        try:

            if action == "new_game":
                diff = msg.get("difficulty", "medium")
                _validate_difficulty(diff)
                try:
                    puzzle, solution = await generate_puzzle_async(diff)
                except asyncio.TimeoutError:
                    await send_error("Puzzle generation timed out — please try again")
                    continue
                game_state.update(
                    puzzle=puzzle,
                    solution=solution,
                    current=[row[:] for row in puzzle],
                    difficulty=diff,
                    hint_count=0,
                    mistakes=0,
                )
                await send({
                    "type":       "new_game",
                    "puzzle":     puzzle,
                    "difficulty": diff,
                })

            elif action == "place_number":
                if game_state["puzzle"] is None:
                    await send_error("No active game")
                    continue

                row   = _require_int(msg.get("row"),   "row",   0, 8)
                col   = _require_int(msg.get("col"),   "col",   0, 8)
                value = _require_int(msg.get("value"), "value", 0, 9)

                if game_state["puzzle"][row][col] != 0:
                    await send_error("Cell is fixed")
                    continue

                game_state["current"][row][col] = value
                try:
                    statuses, solution_board = await validate_board_async(
                        game_state["puzzle"], game_state["current"]
                    )
                except asyncio.TimeoutError:
                    await send_error("Validation timed out")
                    continue

                if value != 0 and value != solution_board[row][col]:
                    game_state["mistakes"] += 1

                await send({
                    "type":     "update",
                    "row":      row,
                    "col":      col,
                    "value":    value,
                    "statuses": statuses,
                    "complete": is_complete(game_state["current"], solution_board),
                    "mistakes": game_state["mistakes"],
                })

            elif action == "validate":
                if game_state["puzzle"] is None:
                    await send_error("No active game")
                    continue
                try:
                    statuses, solution_board = await validate_board_async(
                        game_state["puzzle"], game_state["current"]
                    )
                except asyncio.TimeoutError:
                    await send_error("Validation timed out")
                    continue
                await send({
                    "type":     "validation",
                    "statuses": statuses,
                    "complete": is_complete(game_state["current"], solution_board),
                    "mistakes": game_state["mistakes"],
                    "solution": solution_board,
                })

            elif action == "hint":
                if game_state["puzzle"] is None:
                    await send_error("No active game")
                    continue
                empties = [(r, c) for r in range(9) for c in range(9)
                           if game_state["current"][r][c] == 0]
                if not empties:
                    await send({"type": "hint", "hint": None})
                    continue
                sol = game_state["solution"]
                r, c = random.choice(empties)
                hint = {"row": r, "col": c, "value": sol[r][c]}
                game_state["current"][r][c] = hint["value"]
                game_state["hint_count"] += 1
                try:
                    statuses, _ = await validate_board_async(
                        game_state["puzzle"], game_state["current"]
                    )
                except asyncio.TimeoutError:
                    await send_error("Validation timed out")
                    continue
                await send({
                    "type":       "hint",
                    "hint":       hint,
                    "statuses":   statuses,
                    "complete":   is_complete(game_state["current"], sol),
                    "hint_count": game_state["hint_count"],
                })

            elif action == "cheat":
                if game_state["puzzle"] is None:
                    await send_error("No active game")
                    continue
                cur = game_state["current"]
                all_notes = [
                    [[n for n in range(1, 10) if is_valid(cur, r, c, n)]
                     if cur[r][c] == 0 else []
                     for c in range(9)]
                    for r in range(9)
                ]
                await send({"type": "cheat", "notes": all_notes})

            elif action == "save_game":
                if game_state["puzzle"] is None:
                    await send_error("No active game")
                    continue
                notes_tokens = msg.get("notes_flat", [])
                try:
                    _validate_notes_flat(notes_tokens)
                except ValidationError as e:
                    await send_error(f"Invalid notes_flat: {e}")
                    continue
                sol = game_state["solution"]
                await send({
                    "type":       "saved_state",
                    "puzzle":     to_line(game_state["puzzle"]),
                    "solution":   to_line(sol),
                    "current":    to_line(game_state["current"]),
                    "difficulty": game_state["difficulty"],
                    "mistakes":   game_state["mistakes"],
                    "hint_count": game_state["hint_count"],
                    "notes_flat": notes_tokens,
                })

            elif action == "load_game":
                try:
                    raw_puzzle  = _require_81_digit_string(
                        msg.get("puzzle",  ""), "puzzle",  allow_zero=True)
                    raw_current = _require_81_digit_string(
                        msg.get("current", ""), "current", allow_zero=True)
                    _validate_puzzle_consistency(raw_puzzle, raw_current)
                    _validate_no_conflicts(raw_puzzle,  "puzzle")
                    _validate_no_conflicts(raw_current, "current")
                    diff       = _validate_difficulty(msg.get("difficulty", "medium"))
                    mistakes   = _require_int(msg.get("mistakes",   0), "mistakes",   0, 9999)
                    hint_count = _require_int(msg.get("hint_count", 0), "hint_count", 0, 9999)
                    notes_tokens = _validate_notes_flat(msg.get("notes_flat", ['.']*81))
                except ValidationError as e:
                    await send_error(f"Invalid save data: {e}")
                    continue

                # Verify puzzle is actually solvable
                test_board = from_line(raw_puzzle)
                try:
                    solved_ok = await solve_board_async(test_board)
                except asyncio.TimeoutError:
                    await send_error("Invalid save data: puzzle solver timed out")
                    continue
                if not solved_ok:
                    await send_error("Invalid save data: puzzle has no solution")
                    continue

                puzzle  = from_line(raw_puzzle)
                current = from_line(raw_current)
                try:
                    statuses, solution_board = await validate_board_async(puzzle, current)
                except asyncio.TimeoutError:
                    await send_error("Validation timed out")
                    continue

                game_state.update(
                    puzzle=puzzle,
                    solution=solution_board,
                    current=current,
                    difficulty=diff,
                    hint_count=hint_count,
                    mistakes=mistakes,
                )
                await send({
                    "type":       "loaded_game",
                    "puzzle":     raw_puzzle,
                    "current":    raw_current,
                    "statuses":   statuses,
                    "difficulty": diff,
                    "mistakes":   mistakes,
                    "hint_count": hint_count,
                    "notes_flat": notes_tokens,
                })

            elif action == "reset":
                if game_state["puzzle"] is None:
                    await send_error("No active game")
                    continue
                game_state["current"]  = [row[:] for row in game_state["puzzle"]]
                game_state["mistakes"] = 0
                await send({"type": "reset", "puzzle": game_state["puzzle"]})

            else:
                await send_error("Unknown action")

        except ValidationError as e:
            await send_error(str(e))
        except Exception:
            # Never leak internal tracebacks to the client
            await send_error("An internal error occurred")


async def main():
    print("Sudoku WebSocket server starting on ws://localhost:8765")
    async with websockets.serve(handler, "localhost", 8765):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
