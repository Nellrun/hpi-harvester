# ТЗ: `hpi-harvester` MVP

## Контекст и цель

Написать тонкий оркестратор на Python, который по расписанию запускает произвольные CLI-экспортёры и складывает их вывод в стандартизированную файловую структуру. Оркестратор упаковывается в Docker-контейнер, работает как демон с встроенным шедулером.

Цель MVP — рабочая система с одним экспортёром: **Last.fm**. Использует готовую тулзу [`lastfm-backup`](https://github.com/karlicoss/lastfm-backup) от karlicoss, которая печатает JSON со скробблами в stdout.

Проект должен быть спроектирован расширяемо — добавление нового экспортёра должно сводиться к дописыванию блока в YAML-конфиг без правки кода harvester'а.

## Стек

- **Python 3.12**
- **Typer** — CLI
- **Pydantic v2** — валидация YAML-конфига
- **APScheduler 3.x** — планировщик (BlockingScheduler)
- **PyYAML** — парсинг конфига
- **SQLite** через stdlib `sqlite3` — state (last_run, last_status)
- **Docker + docker-compose**
- **uv** или **pip** — менеджер пакетов (на усмотрение)

## Структура проекта

```
hpi-harvester/
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .gitignore
├── README.md
├── examples/
│   ├── harvester.yaml              # пример конфига с lastfm
│   └── secrets/
│       └── lastfm.key.example      # пример файла с ключом
├── src/
│   └── harvester/
│       ├── __init__.py
│       ├── __main__.py             # python -m harvester → cli
│       ├── cli.py                  # Typer commands
│       ├── config.py               # Pydantic модели
│       ├── runner.py               # subprocess + output modes
│       ├── scheduler.py            # APScheduler wrapper
│       ├── state.py                # SQLite
│       ├── logging_setup.py        # настройка логгеров
│       └── storage.py              # пути, имена снапшотов, атомарность
└── tests/
    ├── __init__.py
    ├── test_config.py
    ├── test_runner.py
    └── test_storage.py
```

## Модель конфига (`config.py`)

Pydantic-модели, загружаются из YAML. Поддерживаемые типы полей строго соответствуют ниже описанному — никаких "на будущее" и опциональных хитростей без необходимости.

```python
from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional
from pathlib import Path

class OutputConfig(BaseModel):
    mode: Literal['stdout', 'argument', 'cwd']
    # Для stdout — расширение итогового файла (json, csv, ...)
    extension: Optional[str] = None
    # Для argument/cwd — тип вывода: одиночный файл или папка
    format: Optional[Literal['file', 'directory']] = None

    @field_validator('extension')
    @classmethod
    def _check_extension(cls, v, info):
        if info.data.get('mode') == 'stdout' and not v:
            raise ValueError("output.extension is required when mode=stdout")
        return v

    @field_validator('format')
    @classmethod
    def _check_format(cls, v, info):
        if info.data.get('mode') in ('argument', 'cwd') and not v:
            raise ValueError("output.format is required when mode=argument/cwd")
        return v


class ExporterConfig(BaseModel):
    name: str = Field(..., pattern=r'^[a-z0-9_-]+$')
    command: str
    schedule: str  # crontab syntax: "0 3 * * *"
    output: OutputConfig
    timeout_seconds: int = 1800  # 30 минут дефолт
    # Переменные окружения для передачи в subprocess
    env: dict[str, str] = Field(default_factory=dict)


class HarvesterConfig(BaseModel):
    output_root: Path
    timezone: str = 'UTC'
    log_dir: Optional[Path] = None  # default: <output_root>/.harvester/logs
    state_db: Optional[Path] = None  # default: <output_root>/.harvester/state.db
    exporters: list[ExporterConfig]

    @field_validator('exporters')
    @classmethod
    def _unique_names(cls, v):
        names = [e.name for e in v]
        if len(names) != len(set(names)):
            raise ValueError("Exporter names must be unique")
        return v

    def resolve_paths(self) -> 'HarvesterConfig':
        """Заполняет дефолтные пути на основе output_root после загрузки."""
        if self.log_dir is None:
            self.log_dir = self.output_root / '.harvester' / 'logs'
        if self.state_db is None:
            self.state_db = self.output_root / '.harvester' / 'state.db'
        return self


def load_config(path: Path) -> HarvesterConfig:
    import yaml
    with path.open() as f:
        data = yaml.safe_load(f)
    config = HarvesterConfig.model_validate(data)
    return config.resolve_paths()
```

**Важно**: в YAML пользователь может указать переменные окружения через `env:` — это нужно для передачи API-ключа напрямую (как в требовании "и то и то"). Пример конфига покажет оба варианта.

## Секреты Last.fm — два способа

Пользователь выбирает один из двух вариантов для передачи API-ключа:

**Вариант 1: файл с ключом, смонтированный в контейнер**

```yaml
# harvester.yaml
exporters:
  - name: lastfm
    command: lastfm-backup --api-key-file /secrets/lastfm.key --user daniil
    # ...
```

Файл `/secrets/lastfm.key` содержит только API-ключ одной строкой.

**Вариант 2: переменная окружения**

```yaml
exporters:
  - name: lastfm
    command: lastfm-backup --api-key "$LASTFM_API_KEY" --user daniil
    env:
      LASTFM_API_KEY: "${LASTFM_API_KEY}"  # подтягивается из env контейнера
```

Переменная `LASTFM_API_KEY` задаётся через `docker-compose.yml` + `.env` файл в корне проекта (стандартный механизм Docker Compose).

**Важный момент реализации**: harvester должен уметь раскрывать `${VAR}` в значениях `env` — пусть это будет `os.path.expandvars()` при загрузке конфига. Если переменной нет — оставлять как есть (не падать), но логировать warning.

## Runner (`runner.py`)

Ядро системы. Функция `run_exporter(exporter, config, state)` делает:

1. Генерирует timestamp: `datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%S')`
2. Создаёт папку `<output_root>/<exporter.name>/` если не существует
3. Определяет путь вывода в зависимости от `output.mode`:
   - `stdout` → `<service_dir>/<timestamp>.<extension>`
   - `argument` с `format=file` → `<service_dir>/<timestamp>.dat` (расширение не важно, решает экспортёр)
   - `argument` с `format=directory` → `<service_dir>/<timestamp>/`
   - `cwd` — аналогично `argument`
4. Выполняет `subprocess.run` согласно режиму (см. ниже)
5. При успехе атомарно переименовывает tmp → финальное имя
6. При неуспехе — удаляет tmp, пишет в state, поднимает исключение
7. Записывает в лог-файл `<log_dir>/<exporter.name>/<timestamp>.log` — команду, stderr экспортёра, код возврата, длительность
8. Обновляет state в SQLite

### Три output-режима — детали

**Режим `stdout`:**

```python
tmp_path = service_dir / f".{timestamp}.{extension}.tmp"
final_path = service_dir / f"{timestamp}.{extension}"

with tmp_path.open('wb') as f:
    result = subprocess.run(
        exporter.command,
        stdout=f,
        stderr=subprocess.PIPE,
        shell=True,
        env={**os.environ, **exporter.env},
        timeout=exporter.timeout_seconds,
    )

if result.returncode == 0:
    tmp_path.rename(final_path)
else:
    tmp_path.unlink(missing_ok=True)
    raise ExporterFailed(result.returncode, result.stderr.decode())
```

**Режим `argument`:**

Команда содержит плейсхолдер `{OUTPUT}`, harvester подставляет путь. Для `format=file` создаёт пустой tmp-файл, для `format=directory` — tmp-папку.

```python
tmp_path = service_dir / f".{timestamp}.tmp"
final_path = service_dir / timestamp  # без расширения для directory

if output.format == 'directory':
    tmp_path.mkdir()
# для file — не создаём, экспортёр создаст сам

command = exporter.command.replace('{OUTPUT}', str(tmp_path))
result = subprocess.run(command, shell=True, ...)

if result.returncode == 0:
    tmp_path.rename(final_path)
else:
    if tmp_path.exists():
        shutil.rmtree(tmp_path) if tmp_path.is_dir() else tmp_path.unlink()
    raise ExporterFailed(...)
```

Если в `command` нет `{OUTPUT}` при `mode=argument` — валидация конфига должна упасть.

**Режим `cwd`:**

Запуск в `tempfile.mkdtemp()`, после успеха — `shutil.move` в финальное место.

```python
import tempfile
tmp_parent = tempfile.mkdtemp(prefix=f'harvester_{exporter.name}_')
try:
    result = subprocess.run(
        exporter.command,
        shell=True,
        cwd=tmp_parent,
        ...
    )
    if result.returncode == 0:
        shutil.move(tmp_parent, service_dir / timestamp)
    else:
        raise ExporterFailed(...)
finally:
    if Path(tmp_parent).exists():  # если move не случился
        shutil.rmtree(tmp_parent, ignore_errors=True)
```

### Исключения

```python
class HarvesterError(Exception): pass
class ExporterFailed(HarvesterError):
    def __init__(self, returncode: int, stderr: str):
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"Exporter failed with code {returncode}: {stderr[:500]}")
class ExporterTimeout(HarvesterError): pass
```

## State (`state.py`)

SQLite-база для отслеживания запусков. Одна таблица:

```sql
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exporter TEXT NOT NULL,
    started_at TEXT NOT NULL,       -- ISO 8601
    finished_at TEXT,
    status TEXT NOT NULL,            -- 'running' | 'success' | 'failed' | 'timeout'
    output_path TEXT,
    log_path TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_exporter ON runs(exporter, started_at DESC);
```

API:

```python
class State:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def start_run(self, exporter: str) -> int:
        """Returns run_id"""

    def finish_run(self, run_id: int, status: str, output_path: Optional[Path], 
                   log_path: Path, error: Optional[str] = None) -> None: ...

    def last_run(self, exporter: str) -> Optional[dict]:
        """Для CLI команды `harvester status`"""
```

## Scheduler (`scheduler.py`)

```python
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

def run_daemon(config: HarvesterConfig) -> None:
    tz = pytz.timezone(config.timezone)
    scheduler = BlockingScheduler(timezone=tz)
    state = State(config.state_db)
    
    for exporter in config.exporters:
        scheduler.add_job(
            func=_run_with_error_handling,
            trigger=CronTrigger.from_crontab(exporter.schedule, timezone=tz),
            args=[exporter, config, state],
            id=exporter.name,
            name=exporter.name,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        logger.info(f"Scheduled {exporter.name}: {exporter.schedule}")
    
    logger.info(f"Harvester started with {len(config.exporters)} exporters")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Harvester shutting down")

def _run_with_error_handling(exporter, config, state):
    """Обёртка, чтобы исключение в одном экспортёре не валило планировщик."""
    try:
        run_exporter(exporter, config, state)
    except Exception as e:
        logger.exception(f"Exporter {exporter.name} crashed")
        # state уже обновлён внутри run_exporter если исключение было ExporterFailed
```

`max_instances=1` — критично: если экспортёр ещё работает (завис), новый запуск не стартует.

## Logging (`logging_setup.py`)

Два уровня логов:

**Глобальный лог harvester'а** — stdout контейнера + файл `<log_dir>/harvester.log` (rotating, 10MB × 5). Формат: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`.

**Лог каждого запуска экспортёра** — отдельный файл `<log_dir>/<exporter>/<timestamp>.log` с:
- Командой и env
- stderr экспортёра (полностью, не обрезая)
- Временем старта/финиша, длительностью
- Итоговым статусом и кодом возврата

Функция `setup_logging(config)` вызывается при старте.

## CLI (`cli.py`)

Через Typer, минимальный набор команд:

```python
import typer
app = typer.Typer()

@app.command()
def run(config_path: Path = typer.Option('/config/harvester.yaml', '--config', '-c')):
    """Запустить harvester как демон."""
    config = load_config(config_path)
    setup_logging(config)
    run_daemon(config)

@app.command('run-once')
def run_once(
    exporter_name: str,
    config_path: Path = typer.Option('/config/harvester.yaml', '--config', '-c'),
):
    """Запустить один экспортёр разово, для отладки."""
    config = load_config(config_path)
    setup_logging(config)
    exporter = next((e for e in config.exporters if e.name == exporter_name), None)
    if not exporter:
        typer.echo(f"Exporter '{exporter_name}' not found", err=True)
        raise typer.Exit(1)
    state = State(config.state_db)
    run_exporter(exporter, config, state)

@app.command()
def status(
    config_path: Path = typer.Option('/config/harvester.yaml', '--config', '-c'),
):
    """Показать статус последних запусков."""
    config = load_config(config_path)
    state = State(config.state_db)
    for exporter in config.exporters:
        last = state.last_run(exporter.name)
        if last:
            typer.echo(f"{exporter.name}: {last['status']} at {last['finished_at']}")
        else:
            typer.echo(f"{exporter.name}: never ran")

@app.command('validate-config')
def validate_config(
    config_path: Path = typer.Argument(...),
):
    """Проверить YAML на валидность."""
    try:
        load_config(config_path)
        typer.echo("OK")
    except Exception as e:
        typer.echo(f"Invalid: {e}", err=True)
        raise typer.Exit(1)

if __name__ == '__main__':
    app()
```

## Dockerfile

```dockerfile
FROM python:3.12-slim

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сам harvester
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Экспортёры — устанавливаются отдельным слоем для кеширования
RUN pip install --no-cache-dir \
    git+https://github.com/karlicoss/lastfm-backup

# Дефолтные volume mounts
VOLUME ["/config", "/secrets", "/data"]

ENTRYPOINT ["harvester"]
CMD ["run", "--config", "/config/harvester.yaml"]
```

## docker-compose.yml

```yaml
services:
  harvester:
    build: .
    container_name: hpi-harvester
    restart: unless-stopped
    environment:
      - TZ=Europe/Madrid
      # Опционально — если используется env-вариант для секрета
      - LASTFM_API_KEY=${LASTFM_API_KEY:-}
    volumes:
      - ./config:/config:ro
      - ./secrets:/secrets:ro
      - ${HPI_DATA_DIR:-./data}:/data
```

## pyproject.toml

```toml
[build-system]
requires = ["setuptools>=61", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "hpi-harvester"
version = "0.1.0"
description = "Personal data exports orchestrator"
requires-python = ">=3.12"
dependencies = [
    "typer>=0.12",
    "pydantic>=2.5",
    "pyyaml>=6.0",
    "apscheduler>=3.10,<4.0",
    "pytz",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-cov",
    "ruff",
]

[project.scripts]
harvester = "harvester.cli:app"

[tool.setuptools.packages.find]
where = ["src"]

[tool.ruff]
line-length = 100
target-version = "py312"
```

## Пример `examples/harvester.yaml`

```yaml
output_root: /data
timezone: Europe/Madrid

exporters:
  # Вариант с файлом секрета
  - name: lastfm
    command: lastfm-backup --api-key-file /secrets/lastfm.key --user daniil
    schedule: "0 3 * * *"
    output:
      mode: stdout
      extension: json
    timeout_seconds: 600

  # Альтернативный вариант через env (закомментирован)
  # - name: lastfm
  #   command: lastfm-backup --api-key "$LASTFM_API_KEY" --user daniil
  #   schedule: "0 3 * * *"
  #   env:
  #     LASTFM_API_KEY: "${LASTFM_API_KEY}"
  #   output:
  #     mode: stdout
  #     extension: json
```

## Тесты (минимум)

**`test_config.py`**: валидный YAML парсится, невалидный (дубликаты имён, неправильный mode, отсутствие extension при stdout) — падает с понятной ошибкой.

**`test_runner.py`**: прогон на фейковом экспортёре (баш-скрипт типа `echo '{"hello": "world"}'`), проверка что файл появился в правильном месте с правильным именем, проверка атомарности (при `exit 1` — tmp удалён, финального файла нет).

**`test_storage.py`**: проверка генерации timestamp и путей.

## README.md — обязательные разделы

1. **Что это** — одна-две строки
2. **Quickstart** — пошагово:
   - `git clone`
   - Получить API-ключ Last.fm на `https://www.last.fm/api/account/create`
   - `mkdir -p config secrets data`
   - `echo "ваш_ключ" > secrets/lastfm.key`
   - Скопировать `examples/harvester.yaml` → `config/harvester.yaml`, указать свой `--user`
   - `docker compose up -d`
   - Проверить: `docker compose exec harvester harvester run-once lastfm`
   - Увидеть результат в `./data/lastfm/<timestamp>.json`
3. **Структура вывода** — что и куда пишется
4. **Добавление нового экспортёра** — три режима вывода с примерами
5. **Troubleshooting** — как посмотреть логи, как запустить разово, как валидировать конфиг
6. **Интеграция с HPI** — короткий пример `my.lastfm` config, указывающий на `./data/lastfm/*.json`

## Что НЕ входит в MVP (явно)

Чтобы не было соблазна усложнять — вот список того, что делать **не надо**, но стоит держать в уме для v2:

- Telegram/email/Slack уведомления
- Ротация старых снапшотов (`keep_last`)
- Дедупликация идентичных снапшотов по хэшу
- Веб-интерфейс / дашборд
- Ретраи при падении (сейчас — одна попытка, падение пишется в лог, следующий запуск по расписанию)
- Инкрементальные экспорты (передача пути предыдущего дампа в команду)
- Поддержка нескольких экспортёров кроме lastfm
- Health checks для Docker
- Prometheus метрики

## Критерии приёмки MVP

1. `docker compose up -d` запускает контейнер, он не падает
2. `docker compose logs harvester` показывает "Scheduled lastfm: 0 3 * * *" и "Harvester started..."
3. `docker compose exec harvester harvester run-once lastfm` выполняется успешно и создаёт файл `/data/lastfm/<timestamp>.json` с валидным JSON
4. `docker compose exec harvester harvester status` показывает `lastfm: success at ...`
5. Если сломать API-ключ — `run-once` падает с понятным сообщением, лог запуска сохранён в `/data/.harvester/logs/lastfm/<timestamp>.log`, state показывает `failed`
6. В `/data/lastfm/` нет `.tmp` файлов после падения
7. `harvester validate-config examples/harvester.yaml` проходит
8. Юнит-тесты зелёные: `pytest` в корне проекта
