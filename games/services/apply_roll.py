from __future__ import annotations
from time import sleep
from games.services.entry_step_result import EntryStepResult
from games.services.openai_client import OpenAIClient
from games.models import Game, Move
from games.services.game_summary import collect_game_summary, render_summary_prompt
import games.services.game_utils as utils



def at_first_start(rolled, game: Game, six_count, player_id):
    # keep collecting sixes until we see a non-6
    if rolled == 6:
        game.current_six_number = six_count + 1
        game.save(update_fields=["current_six_number"])
        return EntryStepResult(
            status="continue",
            message=f"Випала {game.current_six_number}-та шістка. Кидайте далі!",
            six_count=game.current_six_number,
            moves=[],
        )

    # we got the first non-6 at start → apply combo rule
    if six_count == 0:
        # no six yet — still waiting for the very first 6
        return EntryStepResult(
            status="ignored",
            message=utils.wait_six_msg(rolled=rolled),
            six_count=0,
            moves=[],
        )

    combo = six_count  # number of 6s collected
    move_no = utils.next_move_number(game)
    created_moves: list[Move] = []

    # Build absolute target cells according to the images:
    # 1×6 + X:   0→1→6→(6+X)
    # 2×6 + X:   0→1→6→(6+X)      (X is applied from cell 6, ladders/snakes work)
    # 3×6 + X:   0→1→(1+X)        (ignore all 6s, move only by X from cell 1)
    # 4+×6 + X:  0→1→(sum of all numbers)  (one big move)
    if combo == 1:
        targets = [1, 6, 6 + rolled]
    elif combo == 2:
        targets = [1, 6, 6 + rolled]
    elif combo == 3:
        targets = [1, 1 + rolled]
    else:
        total = combo * 6 + rolled  # e.g. 6+6+6+6+4 = 28
        # 0 -> 1 (normal)
        final_cell_1, chain_1, _ = utils.walk_n_steps(0, 1)
        last_no, m1 = utils.create_moves_with_chain(
            game=game,
            start_move_no=move_no,
            from_cell=0,
            rolled=6,
            final_cell=final_cell_1,
            chain=chain_1,
            on_hold=False,
            at_start=True,
        )
        created_moves.extend(m1)
        move_no = last_no + 1

        # 1 -> 1+total (long move, NO RULES)
        final_cell_2, chain_2, hit_exit = utils.walk_pure_no_rules(1, total)
        last_no, m2 = utils.create_moves_with_chain(
            game=game,
            start_move_no=move_no,
            from_cell=1,
            rolled=int(total),  # show the sum in admin/telegram
            final_cell=final_cell_2,
            chain=chain_2,  # must be [] here
            on_hold=False,
            at_start=True,
        )
        created_moves.extend(m2)

        # mark the long move explicitly in DB so Admin shows it
        if m2:
            type(m2[0]).objects.filter(pk=m2[0].pk).update(
                event_type=utils.et("LONG_MOVE"),
                note=f"Довгий хід: {combo}×6 + {rolled} = {total}",
            )

        prev = final_cell_2

    # короткие сегменты по targets
    prev = 0
    for tgt in targets:
        steps = int(tgt) - int(prev)
        final_cell, chain, hit_exit = utils.walk_n_steps(prev, steps)
        last_no, mvs = utils.create_moves_with_chain(
            game=game,
            start_move_no=move_no,
            from_cell=prev,
            rolled=int(rolled),  # same rolled value for this segment
            final_cell=final_cell,
            chain=chain,
            on_hold=False,  # these are confirmed moves
            at_start=True,
        )
        created_moves.extend(mvs)
        move_no = last_no + 1
        prev = final_cell

        if final_cell == EntryStepResult.EXIT_CELL or final_cell == EntryStepResult.FINISH_CELL or hit_exit:
            utils.persist_finished_record(game, moves=created_moves, reason="finish", player_id=player_id)
            utils.mark_finished_nonactive(game)
            try:
                summary = collect_game_summary(game)
                client = OpenAIClient()
                analysis = client.send_summary_json(summary)
                sleep(3.0)
            except Exception:
                analysis = ""
            return EntryStepResult(
                status="finished",
                message=utils.finish_message(final_cell, analysis),
                six_count=0,
                moves=utils.serialize_moves(created_moves, player_id=player_id),
            )

    # persist end of combo
    game.current_cell = prev
    game.current_six_number = 0
    game.last_move_number = move_no - 1
    game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])

    return EntryStepResult(
        status="single",
        message=f"Комбінація: {combo}×6 + {rolled} застосована.",
        six_count=0,
        moves=utils.serialize_moves(created_moves, player_id=player_id),
    )


def at_start_no_series_active(rolled, game: Game, current_cell, player_id):
    if rolled != 6:
        return EntryStepResult(
            status="ignored",
            message=utils.wait_six_msg(rolled=rolled),
            six_count=0,
            moves=[],
        )

    move_no = utils.next_move_number(game)
    final_cell, chain, hit_exit = utils.walk_n_steps(0, 6)

    last_no, created_moves = utils.create_moves_with_chain(
        game=game,
        start_move_no=move_no,
        from_cell=current_cell,  # 0
        rolled=6,
        final_cell=final_cell,
        chain=chain,
        on_hold=True,
        at_start=True,
    )

    game.current_cell = final_cell
    game.current_six_number = 1
    game.last_move_number = last_no
    game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])

    if hit_exit:
        return utils.finish_game_and_release(game, player_id=player_id)

    return EntryStepResult(
        status="continue",
        message=utils.six_continue_text(game.current_six_number),
        six_count=game.current_six_number,
        moves=[],
    )


def series_active_rolled_six(game: Game, current_cell, player_id, six_count, on_start):
    move_no = utils.next_move_number(game)
    final_cell, chain, hit_exit = utils.walk_n_steps(current_cell, 6)

    last_no, created_moves = utils.create_moves_with_chain(
        game=game,
        start_move_no=move_no,
        from_cell=current_cell,
        rolled=6,
        final_cell=final_cell,
        chain=chain,
        on_hold=True,
        at_start=on_start,
    )

    game.current_cell = final_cell
    game.current_six_number = six_count + 1
    game.last_move_number = last_no
    game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])

    if hit_exit:
        return utils.finish_game_and_release(game, player_id=player_id)

    return EntryStepResult(
        status="continue",
        message=utils.six_continue_text(game.current_six_number),
        six_count=game.current_six_number,
        moves=[],
    )


def no_active_series_rolled_six(game: Game, current_cell, player_id):
    move_no = utils.next_move_number(game)
    final_cell, chain, hit_exit = utils.walk_n_steps(current_cell, 6)

    last_no, created_moves = utils.create_moves_with_chain(
        game=game,
        start_move_no=move_no,
        from_cell=current_cell,
        rolled=6,
        final_cell=final_cell,
        chain=chain,
        on_hold=True,
        at_start=False,
    )

    # если нужно сохранить qa_sequence_in_combo=0 как раньше — проставим на первом созданном ходу
    if created_moves and hasattr(created_moves[0], "qa_sequence_in_combo"):
        type(created_moves[0]).objects.filter(pk=created_moves[0].pk).update(qa_sequence_in_combo=0)

    game.current_cell = final_cell
    game.current_six_number = 1
    game.last_move_number = last_no
    game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])

    if hit_exit:
        return utils.finish_game_and_release(game, player_id=player_id)

    return EntryStepResult(
        status="continue",
        message=utils.six_continue_text(game.current_six_number),
        six_count=game.current_six_number,
        moves=[],
    )


def series_active_rolled_not_six(game: Game, current_cell, player_id, six_count, on_start, rolled):
    # Если буфер уже достиг 68 — завершаем немедленно
    qs_hold = Move.objects.select_for_update().filter(game=game, on_hold=True).order_by("move_number")
    if qs_hold.filter(to_cell=EntryStepResult.EXIT_CELL).exists():
        released_list = list(qs_hold)
        qs_hold.update(on_hold=False)

        # фиксируем позицию и финиш
        game.current_cell = EntryStepResult.EXIT_CELL
        game.current_six_number = 0
        if released_list:
            game.last_move_number = released_list[-1].move_number
            game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])
        else:
            game.save(update_fields=["current_cell", "current_six_number"])

        utils.persist_finished_record(game, moves=released_list, reason="exit_68", player_id=player_id)
        utils.mark_finished_nonactive(game)
        try:
            summary = collect_game_summary(game)
            client = OpenAIClient()
            analysis = client.send_summary_json(summary)
            sleep(3.0)
        except Exception:
            analysis = ""

        return EntryStepResult(
            status="finished",
            message=utils.finish_message(game.current_cell, analysis),
            six_count=0,
            moves=utils.serialize_moves(released_list, player_id=player_id),
        )

    # Комбо внутри игры: 3 шестерки → двигаемся только на X; 4+ → длинный ход без правил
    if (not on_start) and six_count >= 3:
        first_in_series = (
            Move.objects.filter(game=game, on_hold=True).order_by("move_number").first()
        )
        start_cell = int(first_in_series.from_cell if first_in_series else current_cell)

        # сбрасываем буфер on_hold
        Move.objects.filter(game=game, on_hold=True).delete()

        if six_count == 3:
            total_steps = int(rolled)
            move_no = utils.next_move_number(game)
            final_cell, chain, hit_exit = utils.walk_n_steps(start_cell, total_steps)
            shown_roll = total_steps
        else:
            total_steps = six_count * 6 + int(rolled)
            move_no = utils.next_move_number(game)
            final_cell, chain, hit_exit = utils.walk_pure_no_rules(start_cell, total_steps)
            shown_roll = total_steps  # show the sum in admin/telegram

        # persist single combined move
        last_no, created_moves = utils.create_moves_with_chain(
            game=game,
            start_move_no=move_no,
            from_cell=start_cell,
            rolled=int(shown_roll),
            final_cell=final_cell,
            chain=chain,
            on_hold=False,
            at_start=False,
        )

        # помечаем длинный ход
        if created_moves:
            type(created_moves[0]).objects.filter(pk=created_moves[0].pk).update(
                event_type=utils.et("LONG_MOVE"),
                note=f"Довгий хід: {six_count}×6 + {rolled} = {shown_roll}",
            )

        game.current_cell = final_cell
        game.current_six_number = 0
        game.last_move_number = last_no
        game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])

        if final_cell == EntryStepResult.EXIT_CELL or final_cell == EntryStepResult.FINISH_CELL or hit_exit:
            utils.persist_finished_record(game, moves=created_moves, reason="exit_68", player_id=player_id)
            utils.mark_finished_nonactive(game)
            try:
                summary = collect_game_summary(game)
                client = OpenAIClient()
                analysis = client.send_summary_json(summary)
                sleep(3.0)
            except Exception:
                analysis = ""
            return EntryStepResult(
                status="finished",
                message=utils.finish_message(game.current_cell, analysis),
                six_count=0,
                moves=utils.serialize_moves(created_moves, player_id=player_id),
            )

        return EntryStepResult(
            status="single",
            message="Комбо з шістками застосовано.",
            six_count=0,
            moves=utils.serialize_moves(created_moves, player_id=player_id),
        )

    # обычный финал серии: добавляем последний бросок X, освобождаем буфер on_hold
    move_no = utils.next_move_number(game)
    final_cell, chain, hit_exit = utils.walk_n_steps(current_cell, int(rolled))

    last_no, created_moves = utils.create_moves_with_chain(
        game=game,
        start_move_no=move_no,
        from_cell=current_cell,
        rolled=int(rolled),
        final_cell=final_cell,
        chain=chain,
        on_hold=True,
        at_start=on_start,
    )

    game.current_cell = final_cell
    game.current_six_number = 0
    game.last_move_number = last_no
    game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])

    qs = Move.objects.select_for_update().filter(game=game, on_hold=True).order_by("move_number")
    released_list = list(qs)
    qs.update(on_hold=False)

    if final_cell == EntryStepResult.EXIT_CELL or final_cell == EntryStepResult.FINISH_CELL or hit_exit:
        # снапшот завершающей серии
        reason = "exit_68" if final_cell == EntryStepResult.EXIT_CELL else "finish_72"
        utils.persist_finished_record(game, moves=released_list, reason=reason, player_id=player_id)
        utils.mark_finished_nonactive(game)
        try:
            summary = collect_game_summary(game)
            client = OpenAIClient()
            analysis = client.send_summary_json(summary)
            sleep(3.0)
        except Exception:
            analysis = ""

        return EntryStepResult(
            status="finished",
            message=utils.finish_message(game.current_cell, analysis),
            six_count=0,
            moves=utils.serialize_moves(released_list, player_id=player_id),
        )

    return EntryStepResult(
        status="completed",
        message="Серія завершена. Віддаємо всі накопичені ходи.",
        six_count=0,
        moves=utils.serialize_moves(released_list, player_id=player_id),
    )


def no_active_series_rolled_not_six(game: Game, current_cell, player_id, rolled):
    move_no = utils.next_move_number(game)
    final_cell, chain, hit_exit = utils.walk_n_steps(current_cell, int(rolled))

    last_no, created_moves = utils.create_moves_with_chain(
        game=game,
        start_move_no=move_no,
        from_cell=current_cell,
        rolled=int(rolled),
        final_cell=final_cell,
        chain=chain,
        on_hold=False,
        at_start=False,
    )

    game.current_cell = final_cell
    game.last_move_number = last_no
    game.save(update_fields=["current_cell", "last_move_number"])

    if final_cell == EntryStepResult.EXIT_CELL or final_cell == EntryStepResult.FINISH_CELL or hit_exit:
        reason = "exit_68" if final_cell == EntryStepResult.EXIT_CELL else "finish_72"
        utils.persist_finished_record(game, moves=created_moves, reason=reason, player_id=player_id)
        utils.mark_finished_nonactive(game)
        try:
            summary = collect_game_summary(game)
            client = OpenAIClient()
            analysis = client.send_summary_json(summary)
            sleep(3.0)
        except Exception:
            analysis = ""

        return EntryStepResult(
            status="finished",
            message=utils.finish_message(game.current_cell, analysis),
            six_count=0,
            moves=utils.serialize_moves(created_moves, player_id=player_id),
        )

    return EntryStepResult(
        status="single",
        message="Хід виконано.",
        six_count=0,
        moves=utils.serialize_moves(created_moves, player_id=player_id),
    )
