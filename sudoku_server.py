#!/usr/bin/env python3
"""Sudoku WebSocket server - handles puzzle generation, validation, and hints."""

import asyncio
import json
import random
import copy
import websockets

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


def solve(board):
    """Backtracking solver. Returns True if solved, mutates board in-place."""
    for row in range(9):
        for col in range(9):
            if board[row][col] == 0:
                nums = list(range(1, 10))
                random.shuffle(nums)
                for num in nums:
                    if is_valid(board, row, col, num):
                        board[row][col] = num
                        if solve(board):
                            return True
                        board[row][col] = 0
                return False
    return True


def generate_full_board():
    """Generate a complete, valid Sudoku solution."""
    board = [[0] * 9 for _ in range(9)]
    solve(board)
    return board


def count_solutions(board, limit=2):
    """Count solutions up to limit to verify uniqueness."""
    count = [0]

    def _solve(b):
        if count[0] >= limit:
            return
        for row in range(9):
            for col in range(9):
                if b[row][col] == 0:
                    for num in range(1, 10):
                        if is_valid(b, row, col, num):
                            b[row][col] = num
                            _solve(b)
                            b[row][col] = 0
                    return
        count[0] += 1

    _solve([row[:] for row in board])
    return count[0]


CLUE_COUNTS = {"easy": 38, "medium": 30, "hard": 24}


def generate_puzzle(difficulty="medium"):
    """Generate a puzzle with a unique solution at the given difficulty."""
    solution = generate_full_board()
    puzzle = [row[:] for row in solution]

    clues = CLUE_COUNTS.get(difficulty, 30)
    cells = [(r, c) for r in range(9) for c in range(9)]
    random.shuffle(cells)

    removed = 0
    target_remove = 81 - clues

    for row, col in cells:
        if removed >= target_remove:
            break
        backup = puzzle[row][col]
        puzzle[row][col] = 0
        if count_solutions(puzzle) == 1:
            removed += 1
        else:
            puzzle[row][col] = backup

    return puzzle, solution


def validate_board(puzzle, current):
    """
    Return cell-by-cell validation:
      'fixed'   — original clue (not editable)
      'correct' — user entry matches solution
      'wrong'   — user entry conflicts with Sudoku rules
      'empty'   — blank
    """
    # Re-derive solution from the original puzzle
    solution_board = [row[:] for row in puzzle]
    solve(solution_board)

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


def get_hint(puzzle, current):
    """Return one empty cell with its correct value."""
    solution_board = [row[:] for row in puzzle]
    solve(solution_board)
    empties = [(r, c) for r in range(9) for c in range(9) if current[r][c] == 0]
    if not empties:
        return None
    r, c = random.choice(empties)
    return {"row": r, "col": c, "value": solution_board[r][c]}


# ── WebSocket Handler ──────────────────────────────────────────────────────────

async def handler(websocket):
    """Handle a single client connection."""
    game_state = {
        "puzzle": None,
        "solution": None,
        "current": None,
        "difficulty": "medium",
        "hint_count": 0,
        "mistakes": 0,
    }

    async def send(msg: dict):
        await websocket.send(json.dumps(msg))

    async for raw in websocket:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await send({"type": "error", "message": "Invalid JSON"})
            continue

        action = msg.get("type")

        if action == "new_game":
            diff = msg.get("difficulty", "medium")
            puzzle, solution = generate_puzzle(diff)
            game_state.update(
                puzzle=puzzle,
                solution=solution,
                current=[row[:] for row in puzzle],
                difficulty=diff,
                hint_count=0,
                mistakes=0,
            )
            await send({
                "type": "new_game",
                "puzzle": puzzle,
                "difficulty": diff,
            })

        elif action == "place_number":
            if game_state["puzzle"] is None:
                await send({"type": "error", "message": "No active game"})
                continue

            row, col, value = msg.get("row"), msg.get("col"), msg.get("value")
            if not (0 <= row < 9 and 0 <= col < 9):
                await send({"type": "error", "message": "Invalid cell"})
                continue
            if game_state["puzzle"][row][col] != 0:
                await send({"type": "error", "message": "Cell is fixed"})
                continue

            game_state["current"][row][col] = value
            statuses, solution_board = validate_board(
                game_state["puzzle"], game_state["current"]
            )

            # Count new mistakes
            if value != 0 and value != solution_board[row][col]:
                game_state["mistakes"] += 1

            complete = is_complete(game_state["current"], solution_board)

            await send({
                "type": "update",
                "row": row,
                "col": col,
                "value": value,
                "statuses": statuses,
                "complete": complete,
                "mistakes": game_state["mistakes"],
            })

        elif action == "validate":
            if game_state["puzzle"] is None:
                await send({"type": "error", "message": "No active game"})
                continue
            statuses, solution_board = validate_board(
                game_state["puzzle"], game_state["current"]
            )
            complete = is_complete(game_state["current"], solution_board)
            await send({
                "type": "validation",
                "statuses": statuses,
                "complete": complete,
                "mistakes": game_state["mistakes"],
                "solution": solution_board,
            })

        elif action == "cheat":
            if game_state["puzzle"] is None:
                await send({"type": "error", "message": "No active game"})
                continue
            # Build the full candidate list for every empty cell
            all_notes = []
            for r in range(9):
                row_notes = []
                for c in range(9):
                    if game_state["current"][r][c] == 0:
                        candidates = [n for n in range(1, 10)
                                      if is_valid(game_state["current"], r, c, n)]
                        row_notes.append(candidates)
                    else:
                        row_notes.append([])
                all_notes.append(row_notes)
            await send({
                "type": "cheat",
                "notes": all_notes,
            })

        elif action == "hint":
            if game_state["puzzle"] is None:
                await send({"type": "error", "message": "No active game"})
                continue
            hint = get_hint(game_state["puzzle"], game_state["current"])
            if hint is None:
                await send({"type": "hint", "hint": None})
            else:
                game_state["current"][hint["row"]][hint["col"]] = hint["value"]
                game_state["hint_count"] += 1
                statuses, solution_board = validate_board(
                    game_state["puzzle"], game_state["current"]
                )
                complete = is_complete(game_state["current"], solution_board)
                await send({
                    "type": "hint",
                    "hint": hint,
                    "statuses": statuses,
                    "complete": complete,
                    "hint_count": game_state["hint_count"],
                })

        elif action == "save_game":
            if game_state["puzzle"] is None:
                await send({"type": "error", "message": "No active game"})
                continue
            def to_line(board):
                return "".join(str(board[r][c]) for r in range(9) for c in range(9))
            # Derive solution if not already stored
            solution = game_state.get("solution")
            if solution is None:
                solution = [row[:] for row in game_state["puzzle"]]
                solve(solution)
            notes_tokens = msg.get("notes_flat", [])
            await send({
                "type": "saved_state",
                "puzzle":     to_line(game_state["puzzle"]),   # clues only, 0=empty
                "solution":   to_line(solution),               # full solution, no zeros
                "current":    to_line(game_state["current"]),  # player progress
                "difficulty": game_state["difficulty"],
                "mistakes":   game_state["mistakes"],
                "hint_count": game_state["hint_count"],
                "notes_flat": notes_tokens,
            })

        elif action == "load_game":
            raw_puzzle  = msg.get("puzzle",  "")
            raw_current = msg.get("current", "")
            if len(raw_puzzle) != 81 or len(raw_current) != 81:
                await send({"type": "error", "message": "Invalid save data"})
                continue
            def from_line(s):
                return [[int(s[r*9+c]) for c in range(9)] for r in range(9)]
            puzzle  = from_line(raw_puzzle)
            current = from_line(raw_current)
            statuses, solution_board = validate_board(puzzle, current)
            game_state.update(
                puzzle=puzzle,
                solution=solution_board,
                current=current,
                difficulty=msg.get("difficulty", "medium"),
                hint_count=msg.get("hint_count", 0),
                mistakes=msg.get("mistakes", 0),
            )
            await send({
                "type": "loaded_game",
                "puzzle":     raw_puzzle,
                "current":    raw_current,
                "statuses":   statuses,
                "difficulty": game_state["difficulty"],
                "mistakes":   game_state["mistakes"],
                "hint_count": game_state["hint_count"],
                "notes_flat": msg.get("notes_flat", []),
            })

        elif action == "reset":
            if game_state["puzzle"] is None:
                await send({"type": "error", "message": "No active game"})
                continue
            game_state["current"] = [row[:] for row in game_state["puzzle"]]
            game_state["mistakes"] = 0
            await send({
                "type": "reset",
                "puzzle": game_state["puzzle"],
            })

        else:
            await send({"type": "error", "message": f"Unknown action: {action}"})


async def main():
    print("Sudoku WebSocket server starting on ws://localhost:8765")
    async with websockets.serve(handler, "localhost", 8765):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
