"""Offline collection inspection, acceptance and reproducible export."""
import json

from .recording import inspect_episode, review_episode


def configure(parser):
    from pathlib import Path
    commands = parser.add_subparsers(dest="collection_command", required=True)
    inspect = commands.add_parser("inspect", help="Inspect a finalized or crash-interrupted episode")
    inspect.add_argument("episode", type=Path)
    recover = commands.add_parser("recover", help="Inspect readable crash-journal prefix without modifying raw data")
    recover.add_argument("episode", type=Path)
    review = commands.add_parser("review", help="Record acceptance without deleting raw data")
    review.add_argument("episode", type=Path)
    review.add_argument("--outcome", choices=("success", "failure", "interrupted"), required=True)
    review.add_argument("--reject", action="store_true")
    review.add_argument("--note", default="")
    export = commands.add_parser("export", help="Export accepted episodes offline")
    export.add_argument("episodes", nargs="+", type=Path)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--fps", type=int, default=20)
    export.add_argument("--format", choices=("aligned", "lerobot"), default="aligned")
    export.add_argument("--repo-id", default="local/openarm")


def run(args):
    if args.collection_command == "inspect":
        print(json.dumps(inspect_episode(args.episode), indent=2, ensure_ascii=False))
    elif args.collection_command == "recover":
        from .recording import recover_episode
        print(json.dumps(recover_episode(args.episode), indent=2, ensure_ascii=False))
    elif args.collection_command == "review":
        review_episode(args.episode, args.outcome, accepted=not args.reject, note=args.note)
    else:
        from .export import export_aligned, export_lerobot
        if args.format == "lerobot":
            export_lerobot(args.episodes, args.output, args.repo_id, args.fps)
        else:
            if len(args.episodes) != 1:
                raise ValueError("Aligned export takes one episode per output directory")
            export_aligned(args.episodes[0], args.output, args.fps)
