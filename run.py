#!/usr/bin/env python3
"""Serve Agent Office without installing Hermes or changing runtime settings."""
import argparse
import os
from pathlib import Path
import signal
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=os.environ.get('AGENT_OFFICE_PORT', '8113'))
    parser.add_argument('--observer-origin', action='append', default=[], metavar='HTTP(S)://HOST[:PORT]',
                        help='explicit trusted observer forwarding origin (repeatable; does not enable forwarded task runs)')
    parser.add_argument('--enable-task-runner', action='store_true',
                        help='allow this server to launch supported local agent CLIs')
    parser.add_argument('--enable-opencode-handoff', action='store_true',
                        help='allow this server to hand reviewed prompts to one paired, existing OpenCode session')
    parser.add_argument('--enable-opencode-agents', action='store_true',
                        help='allow this server to create and control OpenCode sessions in allowlisted projects')
    parser.add_argument('--project-root', action='append', default=[], metavar='/ABSOLUTE/PROJECT',
                        help='allowlisted project root for managed OpenCode agents (repeatable; requires --enable-opencode-agents)')
    parser.add_argument('--max-active-agents', type=int, default=4,
                        help='maximum number of actively running managed OpenCode agents (default: 4)')
    parser.add_argument('--workspace', action='append', default=[], metavar='/ABSOLUTE/PROJECT',
                        help='allowlisted project directory (repeatable; requires --enable-task-runner)')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    if args.enable_task_runner and not args.workspace:
        parser.error('--enable-task-runner requires at least one --workspace')
    if args.workspace and not args.enable_task_runner:
        parser.error('--workspace requires --enable-task-runner')
    if args.enable_opencode_agents and not args.project_root:
        parser.error('--enable-opencode-agents requires at least one --project-root')
    if args.project_root and not args.enable_opencode_agents:
        parser.error('--project-root requires --enable-opencode-agents')
    if not 1 <= args.max_active_agents <= 16:
        parser.error('--max-active-agents must be between 1 and 16')
    os.environ['AGENT_OFFICE_PORT'] = str(args.port)
    import __init__ as office
    task_runner = None
    if args.enable_task_runner:
        from task_runner import TaskRunner
        try:
            task_runner = TaskRunner([Path(path) for path in args.workspace])
        except ValueError as exc:
            parser.error(str(exc))
    opencode_handoff = None
    if args.enable_opencode_handoff:
        from opencode_handoff import HandoffError, OpenCodeHandoff
        try:
            opencode_handoff = OpenCodeHandoff(office._office_dir() / 'opencode-handoff.json')
        except HandoffError as exc:
            parser.error(exc.message)
    opencode_agents = None
    if args.enable_opencode_agents:
        from opencode_agents import OpenCodeAgents
        try:
            opencode_agents = OpenCodeAgents(
                [Path(path) for path in args.project_root],
                max_active=args.max_active_agents,
                office_dir=office._office_dir(),
            )
        except ValueError as exc:
            parser.error(str(exc))
        opencode_agents.start()
    def stop_on_sigterm(_signum, _frame):
        raise KeyboardInterrupt

    previous_sigterm = None
    if hasattr(signal, 'SIGTERM'):
        previous_sigterm = signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        office._serve(
            task_runner=task_runner,
            opencode_handoff=opencode_handoff,
            opencode_agents=opencode_agents,
            observer_origins=args.observer_origin,
            on_ready=lambda port: print(
                f'Agent Office — http://127.0.0.1:{port} (Ctrl+C to stop)', flush=True
            ),
        )
    except office.ObserverStartupError as exc:
        if opencode_agents is not None:
            opencode_agents.shutdown()
        print(f'Agent Office: {exc}', file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        pass
    finally:
        if opencode_agents is not None:
            opencode_agents.shutdown()
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == '__main__':
    main()
