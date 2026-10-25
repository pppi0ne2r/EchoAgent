from __future__ import annotations

import argparse
import json
from pathlib import Path

from echo_agent.config import DispatcherSettings
from echo_agent.crypto_files import MAX_INPUT_BYTES, safe_input_filename
from echo_agent.crypto_contract import ANSWER_FORMS, HASH_ALGORITHMS, crypto_goal
from echo_agent.dispatcher.client import ProjectClient
from echo_agent.dispatcher.core import Dispatcher


def build_parser() -> argparse.ArgumentParser:
    settings = DispatcherSettings.from_env()
    parser = argparse.ArgumentParser(prog="echo-agent-dispatcher")
    parser.add_argument("--server-url", default=settings.server_url)

    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-project")
    create.add_argument("name", nargs="?")
    create.add_argument("--id", type=int, dest="project_id")
    create.add_argument("--docker")
    create.add_argument("--target-url")
    create.add_argument("--task-type", choices=("general", "crypto"), default="general")
    create.add_argument("--answer-kind", choices=("plaintext", "factors", "private_exponent"))
    create.add_argument("--solution-policy", choices=("unique", "any"))
    create.add_argument("--answer-encoding", choices=("decimal", "hex", "utf8"))
    create.add_argument("--answer-byte-length", type=int)
    create.add_argument("--answer-byte-order", choices=("big", "little"))
    create.add_argument("--verification-mode", choices=("strict", "evidence", "acceptance"))
    create.add_argument("--answer-form", choices=ANSWER_FORMS,
                        help="hash when the task submits a digest of the recovered plaintext")
    create.add_argument("--answer-hash", choices=tuple(sorted(HASH_ALGORITHMS)),
                        help="digest algorithm required by --answer-form hash")
    create.add_argument("--input", action="append", default=[], metavar="ROLE=PATH",
                        help="upload a local file for crypto tasks; repeat for multiple files")
    create.add_argument("--number-radix", type=int, choices=(10, 16))
    create.add_argument("--byte-order", choices=("big", "little"))
    create.add_argument("--byte-length", type=int)
    create.add_argument("--hint", default="")
    create.add_argument("--objective", default="")
    create.add_argument("--activate", action="store_true")

    activate = subparsers.add_parser("activate")
    activate.add_argument("project_id", type=int)

    upload = subparsers.add_parser("upload-input")
    upload.add_argument("project_id", type=int)
    upload.add_argument("path")
    upload.add_argument("--role", default="input")
    upload.add_argument("--input-id")
    upload.add_argument("--number-radix", type=int, choices=(10, 16))
    upload.add_argument("--byte-order", choices=("big", "little"))
    upload.add_argument("--byte-length", type=int)

    show = subparsers.add_parser("show")
    show.add_argument("project_id", type=int)

    accept = subparsers.add_parser("accept-candidate")
    accept.add_argument("project_id", type=int)
    accept.add_argument("--candidate", type=int, required=True, dest="candidate_fact_id")
    accept.add_argument("--operator", required=True)
    accept.add_argument("--note", default="")

    list_projects = subparsers.add_parser("list")
    list_projects.set_defaults(command="list")

    delete_project = subparsers.add_parser("delete-project")
    delete_project.add_argument("project_id", type=int)

    run_once = subparsers.add_parser("run-once")
    run_once.add_argument("project_id", type=int)
    run_once.add_argument("--max-workers", type=int, default=settings.explore_max_workers)
    run_once.add_argument("--codex-model", default=settings.codex_model)
    run_once.add_argument("--codex-first-run-model", default=settings.codex_first_run_model)
    run_once.add_argument("--codex-explore-model", default=settings.codex_explore_model)
    run_once.add_argument("--codex-reason-model", default=settings.codex_reason_model)
    run_once.add_argument("--openai-base-url", default=settings.openai_base_url)

    run_loop = subparsers.add_parser("run-loop")
    run_loop.add_argument("project_id", type=int)
    run_loop.add_argument("--interval", type=float, default=settings.interval_seconds)
    run_loop.add_argument("--max-steps", type=int)
    run_loop.add_argument("--max-workers", type=int, default=settings.explore_max_workers)
    run_loop.add_argument("--codex-model", default=settings.codex_model)
    run_loop.add_argument("--codex-first-run-model", default=settings.codex_first_run_model)
    run_loop.add_argument("--codex-explore-model", default=settings.codex_explore_model)
    run_loop.add_argument("--codex-reason-model", default=settings.codex_reason_model)
    run_loop.add_argument("--openai-base-url", default=settings.openai_base_url)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = ProjectClient(args.server_url, auth_token=DispatcherSettings.from_env().server_auth_token)

    if args.command == "create-project":
        goal = {key: value for key, value in {
            "answer_kind": args.answer_kind, "solution_policy": args.solution_policy,
            "answer_encoding": args.answer_encoding, "byte_length": args.answer_byte_length,
            "byte_order": args.answer_byte_order, "verification_mode": args.verification_mode,
            "answer_form": args.answer_form, "answer_hash": args.answer_hash,
        }.items() if value is not None}
        if goal and args.task_type != "crypto":
            raise ValueError("crypto answer options require --task-type crypto")
        goal_metadata = {"schema_version": 1, "inputs": [], "goal": crypto_goal(goal)} if args.task_type == "crypto" else {}
        if args.input and args.task_type != "crypto":
            raise ValueError("--input requires --task-type crypto")
        input_specs: list[tuple[str, str]] = []
        for spec in args.input:
            role, separator, path = spec.partition("=")
            if not separator or not role or not path:
                raise ValueError("--input must use ROLE=PATH")
            source = Path(path)
            if not source.is_file():
                raise ValueError(f"input file does not exist: {source}")
            if source.stat().st_size > MAX_INPUT_BYTES:
                raise ValueError("input exceeds 32 MiB limit")
            safe_input_filename(source.name)
            input_specs.append((role, path))
        if args.byte_length is not None and args.byte_length < 0:
            raise ValueError("--byte-length must be non-negative")
        project = client.create_project(
            args.name,
            project_id=args.project_id,
            docker=args.docker,
            target_url=args.target_url,
            hint=args.hint,
            objective=args.objective,
            task_type=args.task_type,
            **({"crypto": goal_metadata} if goal_metadata else {}),
        )
        for role, path in input_specs:
            client.upload_input_file(
                project.id, path, role=role, number_radix=args.number_radix,
                byte_order=args.byte_order, byte_length=args.byte_length,
            )
        if input_specs:
            project = client.get_project(project.id)
        if args.activate:
            project = client.activate_project(project.id)
        _print_json(project.to_dict())
        return 0

    if args.command == "upload-input":
        _print_json(client.upload_input_file(
            args.project_id, args.path, role=args.role, input_id=args.input_id,
            number_radix=args.number_radix, byte_order=args.byte_order,
            byte_length=args.byte_length,
        ))
        return 0

    if args.command == "activate":
        _print_json(client.activate_project(args.project_id).to_dict())
        return 0

    if args.command == "show":
        _print_json(client.get_project(args.project_id).to_dict())
        return 0

    if args.command == "accept-candidate":
        _print_json(client.accept_crypto_candidate(
            args.project_id, args.candidate_fact_id, args.operator, note=args.note,
        ))
        return 0

    if args.command == "list":
        _print_json([project.to_dict() for project in client.list_projects()])
        return 0

    if args.command == "delete-project":
        _print_json(client.delete_project(args.project_id).to_dict())
        return 0

    if args.command == "run-once":
        dispatcher = _build_dispatcher(client, args)
        _print_json(dispatcher.run_once(args.project_id).to_dict())
        return 0

    if args.command == "run-loop":
        dispatcher = _build_dispatcher(client, args)
        _print_json(
            [
                result.to_dict()
                for result in dispatcher.run_loop(
                    args.project_id,
                    interval_seconds=args.interval,
                    max_steps=args.max_steps,
                )
            ]
        )
        return 0

    raise ValueError(f"unknown command: {args.command}")


def _build_dispatcher(client: ProjectClient, args: argparse.Namespace) -> Dispatcher:
    settings = DispatcherSettings.from_env()
    mode_models = {
        key: value
        for key, value in {
            "first_run": args.codex_first_run_model,
            "explore": args.codex_explore_model,
            "reason": args.codex_reason_model,
        }.items()
        if value
    }
    return Dispatcher(
        client,
        max_explore_workers=args.max_workers,
        max_codex_slots=settings.codex_max_slots,
        codex_model=args.codex_model,
        codex_mode_models=mode_models,
        codex_workspace=settings.codex_workspace,
        codex_timeout_seconds=settings.codex_timeout_seconds,
        codex_no_output_timeout_seconds=settings.codex_no_output_timeout_seconds,
        codex_mode_no_output_timeout_seconds=settings.codex_mode_no_output_timeout_seconds,
        codex_mode_timeout_seconds=settings.codex_mode_timeout_seconds,
        openai_api_key=settings.openai_api_key,
        openai_base_url=args.openai_base_url,
        codex_command=settings.codex_command,
        codex_provider_name=settings.codex_provider_name,
        codex_provider_supports_websockets=settings.codex_provider_supports_websockets,
        codex_request_max_retries=settings.codex_request_max_retries,
        codex_stream_max_retries=settings.codex_stream_max_retries,
        codex_stream_idle_timeout_ms=settings.codex_stream_idle_timeout_ms,
    )


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
