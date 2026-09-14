from __future__ import annotations

import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import plotext
from retroflow import FlowchartGenerator

from tfr.plugins import BossViewContext

_HISTOGRAM_LABELS = (
    "Load-bearing code",
    "Agent time",
    "Buttered croissants",
    "Fweps",
    "BDHSVs",
    "BANGs",
    "GOKSELs",
    "Newgigs",
    "Words",
    "Graphs",
)
_FLOW_LABELS = (
    "Network Prefetch",
    "Data Cleansing",
    "Gollum Crept up",
    "Data Fetch",
    "Enrichment",
    "Verification",
    "Weights",
    "Enqueue",
    "Dequeue",
    "Degauss",
    "Content Filtering",
    "State Estimation",
    "Check Firewall",
    "Slosh Corn",
    "Enhance",
    "Lowcode",
)
_FILENAMES = (
    "phrasing.xls",
    "chet-manly.txt",
    "randy-randleson.csv",
    "whatever-farm-animal-of-war.js",
    "theres-no-sink-in-there.txt",
    "m-as-in-mancy.md",
    "coarse-grade-sand.sh",
    "i-shall-fetch-a-rug.bat",
    "babou.py",
    "the-alabama-of-europe.lock",
    "dicks-hatband.toml",
    "johnny-bench.mp3",
    "elisha-otis.md",
    "benoit.ooo",
    "burroughs.md",
    "bailiwick.tf",
    "yes-it-is-other-barry.awk",
    "how-you-get-ants.pdf",
    "reap-the-barry.iso",
    "because-you-shot-me.bin",
    "commodores-tribute-band.wav",
    "adamantium.gpg",
    "hunch-hunch.jpeg",
    "skytanic",
    "roll-of-nickels.gif",
    "nice-read-velmba.pdf",
    "this-new-place-called-anywhere.png",
    "safe-non-flammable-helium.zip",
    "trudy-beekman.pdf",
    "cheese-farm.tf",
    "danger-zone.ini",
    "slightly-darker-black.css",
    "tactleneck.conf",
    "idiots-doing-idiot-things.yml",
    "vodka-gummy-bears.dat",
    "peppermint-patties.cache",
    "double-deuce.mov",
    "pampage.tmp",
)


@dataclass(frozen=True, slots=True)
class _DashboardConfig:
    log_path_template: str
    histogram_seconds: float
    histogram_labels: tuple[str, ...]
    flow_seconds: float
    flow_labels: tuple[str, ...]
    filenames: tuple[str, ...]

    @classmethod
    def parse(cls, config: Mapping[str, Any]) -> _DashboardConfig:
        allowed = {"log_path_template", "histogram", "flow", "files"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(f"unknown tfr.boss setting: {sorted(unknown)[0]}")
        template = config.get("log_path_template", "/var/log/{world}")
        if not isinstance(template, str) or template.count("{world}") != 1:
            raise ValueError("tfr.boss.log_path_template must contain {world} exactly once")
        if len(template) > 200 or re.search(r"[\x00-\x1f\x7f-\x9f]", template):
            raise ValueError("tfr.boss.log_path_template is not safe to display")
        histogram = cls._section(config, "histogram", "labels", _HISTOGRAM_LABELS)
        flow = cls._section(config, "flow", "labels", _FLOW_LABELS)
        files = cls._section(config, "files", "names", _FILENAMES, timed=False)
        if len(histogram[1]) < 2:
            raise ValueError("tfr.boss.histogram.labels requires at least two axis labels")
        if len(flow[1]) < 3:
            raise ValueError("tfr.boss.flow.labels requires at least three component names")
        if any("->" in label or label.lstrip().startswith("#") for label in flow[1]):
            raise ValueError("tfr.boss.flow.labels cannot contain '->' or start with '#'")
        if any(not label.isascii() for label in flow[1]):
            raise ValueError("tfr.boss.flow.labels must use single-column ASCII characters")
        if any(label.casefold() == "profit" for label in flow[1]):
            raise ValueError("tfr.boss.flow.labels cannot use the reserved Profit node")
        for filename in files[1]:
            if "/" in filename or "\\" in filename:
                raise ValueError("tfr.boss filenames cannot contain path separators")
        return cls(
            log_path_template=template,
            histogram_seconds=histogram[0],
            histogram_labels=histogram[1],
            flow_seconds=flow[0],
            flow_labels=flow[1],
            filenames=files[1],
        )

    @staticmethod
    def _section(
        config: Mapping[str, Any],
        section_name: str,
        values_name: str,
        defaults: Sequence[str],
        *,
        timed: bool = True,
    ) -> tuple[float, tuple[str, ...]]:
        raw = config.get(section_name, {})
        if not isinstance(raw, Mapping):
            raise ValueError(f"tfr.boss.{section_name} must be an object")
        allowed = {"include_defaults", values_name}
        if timed:
            allowed.add("seconds")
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"unknown tfr.boss.{section_name} setting: {sorted(unknown)[0]}")
        include_defaults = raw.get("include_defaults", True)
        if not isinstance(include_defaults, bool):
            raise ValueError(f"tfr.boss.{section_name}.include_defaults must be a boolean")
        supplied = raw.get(values_name, ())
        if not isinstance(supplied, Sequence) or isinstance(supplied, (str, bytes)):
            raise ValueError(f"tfr.boss.{section_name}.{values_name} must be an array")
        values: list[str] = list(defaults) if include_defaults else []
        for value in supplied:
            if isinstance(value, str):
                value = value.strip()
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 100
                or re.search(r"[\x00-\x1f\x7f-\x9f]", value)
            ):
                raise ValueError(f"tfr.boss.{section_name}.{values_name} entries must be safe text")
            if value.casefold() not in {existing.casefold() for existing in values}:
                values.append(value)
        if not values:
            raise ValueError(f"tfr.boss.{section_name}.{values_name} cannot be empty")
        seconds = raw.get("seconds", 20 if section_name == "histogram" else 15)
        if timed and (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not 1 <= seconds <= 3600
        ):
            raise ValueError(f"tfr.boss.{section_name}.seconds must be between 1 and 3600")
        return float(seconds), tuple(values)


class BuiltinBossPlugin:
    api_version = 1

    def __init__(self) -> None:
        self._config = _DashboardConfig.parse({})

    def register(self, registrar: Any, config: Mapping[str, Any]) -> None:
        self._config = _DashboardConfig.parse(config)
        registrar.register_boss_view(
            "build-dashboard",
            self._render,
            refresh_interval_seconds=1,
        )
        registrar.register_command(
            "boss",
            self._handle_command,
            help="open or configure the disguised operational dashboard",
        )

    @staticmethod
    async def _handle_command(context: Any, arguments: tuple[str, ...]) -> None:
        try:
            if not arguments:
                await context.activate_boss()
                return
            operation = arguments[0].casefold()
            if operation == "status" and len(arguments) == 1:
                context.notice(context.boss_status())
                return
            if operation in {"cycle", "random"} and len(arguments) == 1:
                context.notice(context.configure_boss(operation))
                return
            if operation == "lock" and len(arguments) == 2:
                context.notice(context.configure_boss("locked", arguments[1]))
                return
        except ValueError as exc:
            context.notice(str(exc))
            return
        context.notice("Usage: /boss [status|cycle|random|lock SCREEN]")

    def _render(self, context: BossViewContext) -> tuple[tuple[str, str], ...]:
        status_lines = self._world_status(context)
        phase_span = self._config.histogram_seconds + self._config.flow_seconds
        phase_index = int(context.elapsed_seconds // phase_span)
        phase_offset = context.elapsed_seconds % phase_span
        histogram_phase = phase_offset < self._config.histogram_seconds
        if not histogram_phase and context.height > 0:
            status_lines = status_lines[: max(0, context.height - 1)]
        available = max(0, context.height - len(status_lines))
        filesystem_height = min(6, max(0, available // 4))
        visualization_height = max(0, available - filesystem_height)
        if histogram_phase:
            visualization = self._histogram(context, visualization_height, phase_index)
            visualization_style = "class:boss.chart"
        else:
            visualization = self._flow(context, visualization_height, phase_index)
            visualization_style = "class:boss"
        filesystem = self._filesystem(context, filesystem_height)
        styled_lines = (
            *(("class:boss", line) for line in status_lines),
            *((visualization_style, line) for line in visualization),
            *(("class:boss", line) for line in filesystem),
        )[: context.height]
        return tuple(
            (style, line + ("\n" if index + 1 < len(styled_lines) else ""))
            for index, (style, line) in enumerate(styled_lines)
        )

    def _world_status(self, context: BossViewContext) -> tuple[str, ...]:
        if not context.worlds:
            return ("Waiting for build log sources...", "")
        maximum = max(1, min(len(context.worlds), max(1, context.height // 4)))
        lines: list[str] = []
        for status in context.worlds[:maximum]:
            world = re.sub(r"[^A-Za-z0-9._-]", "_", status.world)
            path = self._config.log_path_template.replace("{world}", world)
            count = status.received_since_activation
            noun = "line" if count == 1 else "lines"
            idle = (
                f"{int(status.idle_seconds // 60)}m ago"
                if status.idle_seconds is not None
                else "never"
            )
            line = (
                f"New events on {path}: {count} {noun} with last activity {idle}. "
                f"[{status.connection_state}]"
            )
            lines.append(line[: context.width])
        if len(context.worlds) > maximum:
            summary = f"... {len(context.worlds) - maximum} additional log sources"
            lines.append(summary[: context.width])
        lines.append("")
        return tuple(lines)

    def _histogram(
        self,
        context: BossViewContext,
        height: int,
        phase_index: int,
    ) -> tuple[str, ...]:
        if height <= 0:
            return ()
        generator = random.Random(f"{context.seed}:histogram:{phase_index}")
        vertical_label, horizontal_label = generator.sample(
            self._config.histogram_labels,
            2,
        )
        chart_width = min(context.width, 80)
        if chart_width < 30 or height < 8:
            return self._compact_histogram(
                chart_width,
                height,
                vertical_label,
                horizontal_label,
                generator,
            )
        bar_count = max(3, min(5, chart_width // 14))
        x_values = list(range(1, bar_count + 1))
        heights = [generator.randint(18, 96) for _ in x_values]
        x_legend = sorted(generator.sample(range(100, 1000), bar_count))
        y_max = generator.choice((500, 1000, 2000, 5000))

        figure = plotext.figure
        figure.clear()
        figure.plot_size(chart_width, height)
        figure.theme("dark")
        signal = figure.bar(
            x_values,
            heights,
            marker="█",
            orientation="vertical",
            width=0.8,
            lines=True,
            fill=True,
        )
        figure.draw(signal)
        figure.label(horizontal_label, "x")
        figure.label(vertical_label, "y")
        figure.ruler("x").ticks(x_values, [str(value) for value in x_legend])
        figure.ruler("y").lim(0, 100)
        figure.ruler("y").ticks(
            (0, 25, 50, 75, 100),
            tuple(str(round(y_max * value / 100)) for value in (0, 25, 50, 75, 100)),
        )
        rendered = plotext.uncolorize(str(figure.build()))
        return tuple(line[:chart_width] for line in rendered.splitlines()[:height])

    @staticmethod
    def _compact_histogram(
        width: int,
        height: int,
        vertical_label: str,
        horizontal_label: str,
        generator: random.Random,
    ) -> tuple[str, ...]:
        values = [generator.randint(1, max(1, height - 3)) for _ in range(min(5, width // 3))]
        rows = max(values, default=0)
        lines = [vertical_label[:width]]
        for level in range(rows, 0, -1):
            lines.append(" ".join("█" if value >= level else " " for value in values)[:width])
        lines.append(" ".join(str(generator.randint(0, 9)) for _ in values)[:width])
        lines.append(horizontal_label[:width])
        return tuple(lines[:height])

    def _flow(
        self,
        context: BossViewContext,
        height: int,
        phase_index: int,
    ) -> tuple[str, ...]:
        if height <= 0:
            return ()
        generator = random.Random(f"{context.seed}:flow:{phase_index}")
        count = generator.randint(3, min(7, len(self._config.flow_labels)))
        components = generator.sample(self._config.flow_labels, count)
        loop_index = generator.randrange(count)
        available_height = max(0, height - 1)

        for visible_count in range(count, 2, -1):
            visible_components = components[:visible_count]
            loop_target = visible_components[loop_index % visible_count]
            nodes = [*visible_components, "Profit"]
            edges = [*pairwise(nodes), ("Profit", loop_target)]
            source = "\n".join(f"{start} -> {end}" for start, end in edges)
            for direction in ("LR", "TB"):
                for shadow in (True, False):
                    diagram = FlowchartGenerator(
                        max_text_width=16,
                        min_box_width=6,
                        horizontal_spacing=2,
                        vertical_spacing=1,
                        shadow=shadow,
                        rounded=True,
                        direction=direction,
                    ).generate(source)
                    lines = diagram.strip("\n").splitlines()
                    if len(lines) <= available_height and all(
                        len(line) <= context.width for line in lines
                    ):
                        return ("Generated build sequence", *lines)

        loop_target = components[loop_index]
        compact = f"Profit -> {loop_target} (cycle)"[: context.width]
        if height == 1:
            return (compact,)
        return ("Generated build sequence"[: context.width], compact)

    def _filesystem(self, context: BossViewContext, height: int) -> tuple[str, ...]:
        if height <= 0:
            return ()
        generator = random.Random(f"{context.seed}:files")
        filenames = list(self._config.filenames)
        generator.shuffle(filenames)
        lines = ["$ ls -la /opt/build/artifacts"]
        for index, filename in enumerate(filenames[: max(0, height - 1)]):
            executable = filename.endswith((".sh", ".bat", ".awk"))
            mode = "-rwxr-xr-x" if executable else "-rw-r--r--"
            owner = "build" if index % 3 else "runner"
            size = generator.randint(96, 9_999_999)
            hour = generator.randint(0, 23)
            minute = generator.randint(0, 59)
            line = f"{mode}  1 {owner:<6} staff {size:>8} Sep 13 {hour:02d}:{minute:02d} {filename}"
            lines.append(line[: context.width])
        return tuple(lines[:height])
