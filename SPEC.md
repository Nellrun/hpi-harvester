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
│       ├── manifest.py             # _index.json: сбор и атомарная запись
│       └── storage.py              # пути, имена снапшотов, атомарность
└── tests/
    ├── __init__.py
    ├── test_config.py
    ├── test_runner.py
    ├── test_storage.py
    └── test_manifest.py
```

## Публичный контракт для потребителей

Всё, что описано в этом разделе, — **стабильный контракт** между harvester'ом и любым кодом, читающим его вывод (HPI-модули, скрипты, MCP-надстройки). Соблюдение гарантируется между минорными версиями; ломающие изменения требуют `schema_version` bump у `_index.json` и упоминания в CHANGELOG.

Остальные разделы ТЗ — детали реализации; они могут меняться свободно, если контракт соблюдён.

Harvester **не зависит** ни от одного потребителя — ни импортом, ни схемой. HPI знать ничего не знает, Echo не знает, `my.config` не фигурирует. Всё, что потребители получают, — это файлы на диске и манифест рядом с ними.

### Filesystem layout

```
<output_root>/
├── <source_a>/
│   ├── _index.json           ← манифест, если write_manifest=true
│   ├── <timestamp>.<ext>     ← снапшот-файл (output.mode=stdout)
│   └── <timestamp>/          ← снапшот-директория (output.format=directory)
│       └── ...
├── <source_b>/
│   └── ...
└── .harvester/               ← внутреннее состояние, НЕ контракт
    ├── state.db
    └── logs/<source>/<timestamp>.log
```

Пространство имён `<output_root>/.harvester/` — приватное, контракту не принадлежит; потребители не должны на него опираться.

Имя директории источника `<source>` совпадает с `exporters[].name` в YAML-конфиге (ограничено `^[a-z0-9_-]+$`). Для потребителей это — единственный «идентификатор источника».

### Формат timestamp

Единый формат для всех снапшотов:

```
%Y-%m-%dT%H-%M-%S    # 2026-04-20T03-00-00
```

Свойства:
- UTC — всегда, независимо от `timezone` в конфиге (тот влияет только на расписание).
- Секундная точность. Коллизий нет: параллельных запусков одного экспортёра нет (`max_instances=1`).
- Безопасен как имя файла на всех POSIX- и Windows-совместимых ФС (никаких `:`, `/`).
- Лексикографическая сортировка эквивалентна хронологической.

Формат — часть контракта: потребители парсят его напрямую. Изменение требует bump `schema_version`.

### Атомарность записи

Для каждого успешного run'а harvester выполняет **ровно две атомарные операции** в следующем порядке:

1. **Снапшот**: `<service_dir>/.<timestamp>.<ext>.tmp` (hidden) → `<service_dir>/<timestamp>.<ext>` через `rename(2)`.
2. **Манифест** (если `write_manifest=true`): `<service_dir>/._index.json.tmp` → `<service_dir>/_index.json` через `rename(2)`.

Между операциями 1 и 2 состояние следующее: снапшот на диске есть, в манифесте его ещё нет. Если процесс убить в этот момент, следующий успешный run полностью перестроит `snapshots[]` (см. ниже), и файл подхватится. Потеря между run'ами невозможна: источник истины — содержимое директории, манифест — лишь ускоритель.

Имена `.<name>.tmp` всегда скрыты (ведущая точка). Потребители **должны** игнорировать точечные файлы при glob'е директории.

### Manifest (`_index.json`)

Машиночитаемое описание содержимого директории источника. Генерируется harvester'ом, читается потребителями. Формат:

```json
{
  "schema_version": 1,
  "exporter": "lastfm",
  "updated_at": "2026-04-21T03:02:41Z",
  "snapshots": [
    {
      "timestamp": "2026-04-20T03-00-00",
      "path": "2026-04-20T03-00-00.json",
      "type": "file",
      "size_bytes": 1048576,
      "run_started_at": "2026-04-20T03:00:00Z",
      "run_ended_at": "2026-04-20T03:02:41Z"
    }
  ]
}
```

Поля:

- `schema_version` (int, **required**) — `1` в текущей версии. Инкрементируется при любом ломающем изменении формата.
- `exporter` (string, **required**) — имя источника; совпадает с именем директории.
- `updated_at` (string, ISO 8601 UTC с `Z`, **required**) — момент последней успешной записи манифеста.
- `snapshots` (array, **required**) — отсортирован по `timestamp` **по возрастанию** (последний элемент — самый свежий). Пустой массив валиден (сразу после первого создания директории без успешных run'ов).

Объект снапшота:

- `timestamp` (string, **required**) — в формате `%Y-%m-%dT%H-%M-%S` (см. выше).
- `path` (string, **required**) — basename относительно директории источника. Для `stdout`/`argument+file` — `<timestamp>.<ext>` или `<timestamp>`; для `directory` — `<timestamp>` (без слэша).
- `type` (string, **required**) — `"file"` или `"directory"`.
- `size_bytes` (int, **required**) — для `file`: размер файла; для `directory`: сумма размеров всех regular-файлов в поддереве (рекурсивно, симлинки не следуем).
- `run_started_at`, `run_ended_at` (string, ISO 8601 UTC, **optional**) — присутствуют, если run зарегистрирован в `state.db`. Отсутствуют для файлов, положенных вручную или существовавших до появления state.db.

Манифест описывает **только успешные** снапшоты. Failed/timeout-run'ы остаются в `.harvester/logs/<source>/` и в `state.db`, в манифесте их нет.

### Правило перестроения `snapshots[]`

На каждом успешном run'е (если `write_manifest=true`) harvester **полностью перестраивает** массив:

1. Glob `<service_dir>/*` с фильтром: пропустить скрытые (`.`-prefixed), пропустить `_index.json`.
2. Для каждого попавшего под фильтр entry:
   - попытаться распарсить имя как timestamp; если не парсится — **пропустить** (это посторонний файл, не наш снапшот);
   - определить `type` по `is_dir`;
   - посчитать `size_bytes`;
   - найти в state.db строку `status=success` с таким же `output_path` — если есть, подтянуть `run_started_at`/`run_ended_at`; иначе оставить поля отсутствующими.
3. Отсортировать по `timestamp` asc.
4. Записать `<service_dir>/._index.json.tmp` → `<service_dir>/_index.json` атомарно.

Следствие: **ручные** снапшоты (положенные руками, с валидным timestamp-именем) видны потребителям через манифест — без `run_*_at`, но с `size_bytes`. **Посторонние** файлы (README.md, .DS_Store, etc.) невидимы.

### Инварианты, на которые можно полагаться

- Если `_index.json` существует и парсится — он внутренне консистентен (ни один снапшот там не указывает на несуществующий путь *на момент записи*; между записями файл может быть удалён руками).
- Если `_index.json` отсутствует — директория — единственный источник истины; потребитель делает glob сам.
- Снапшот, появившийся в директории, **никогда** не будет частично записан: либо целиком, либо его нет.
- Harvester **никогда не мутирует** существующий снапшот. `<timestamp>`-запись неизменна от момента появления.
- Harvester **никогда не удаляет** снапшоты сам. Ротация — задача внешнего процесса.
- `_index.json` может быть произвольно устаревшим относительно директории (если run'ов давно не было, а файлы добавлялись руками). Потребители, требующие строгой консистентности, могут сверять манифест с `glob` и брать объединение.

### Рекомендуемый паттерн чтения

```python
# Fast path: manifest есть.
manifest = json.loads((service_dir / "_index.json").read_text())
assert manifest["schema_version"] == 1
snapshots = [service_dir / s["path"] for s in manifest["snapshots"]]

# Fallback: манифеста нет, глобим директорию.
snapshots = sorted(
    p for p in service_dir.iterdir()
    if not p.name.startswith(".") and p.name != "_index.json"
)
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
    # Per-exporter override глобального HarvesterConfig.write_manifest.
    # None → наследуем глобальное значение. True/False → принудительно.
    write_manifest: Optional[bool] = None


class HarvesterConfig(BaseModel):
    output_root: Path
    timezone: str = 'UTC'
    log_dir: Optional[Path] = None  # default: <output_root>/.harvester/logs
    state_db: Optional[Path] = None  # default: <output_root>/.harvester/state.db
    # Глобальный дефолт: писать ли _index.json после каждого успешного run'а.
    # По умолчанию False — потребителей, полагающихся на формат, ещё нет,
    # и явное включение эту семантику фиксирует.
    write_manifest: bool = False
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
9. Если эффективный `write_manifest=true` (см. ниже раздел про manifest writer) — перестраивает `_index.json` по правилу из контракта. Сбой этой стадии логируется, но не ломает run: снапшот уже на диске, state.db уже обновлён, источник истины — директория

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

## Manifest writer (`manifest.py`)

Отдельный модуль, чтобы Runner остался тонким. Публичная функция одна:

```python
def update_manifest(
    service_dir: Path,
    exporter_name: str,
    state: State,
) -> None:
    """Rebuild <service_dir>/_index.json from directory contents + state.db.

    Called after a successful snapshot promote. Safe to call when the
    directory already contains manually-placed snapshots: any entry whose
    name is not a valid timestamp is ignored. Non-fatal on failure — the
    directory itself is the source of truth.
    """
```

Эффективное значение `write_manifest` для экспортёра:

```python
def effective_write_manifest(config: HarvesterConfig, exporter: ExporterConfig) -> bool:
    return config.write_manifest if exporter.write_manifest is None else exporter.write_manifest
```

### Алгоритм

1. `entries = [p for p in service_dir.iterdir() if not p.name.startswith('.') and p.name != '_index.json']`
2. Для каждой `p`:
   - выделить timestamp-кандидат: для `is_file` — strip расширения, для `is_dir` — имя as-is;
   - попытаться распарсить через `datetime.strptime(candidate, TIMESTAMP_FORMAT)`; fail → пропустить;
   - `type = 'file' if p.is_file() else 'directory'`;
   - `size_bytes = p.stat().st_size if p.is_file() else sum(c.stat().st_size for c in p.rglob('*') if c.is_file())`;
   - `run_meta = state.find_success_run(exporter_name, output_path=p)` → `(started_at, finished_at)` или `None`.
3. Отсортировать по `timestamp` asc.
4. Собрать payload, сериализовать JSON с `sort_keys=True`, `indent=2`, финальный `\n`.
5. Записать в `.harvester/._index.json.tmp` (нет, внутри `service_dir`: `service_dir / "._index.json.tmp"`) через `open(..., "w")` → `flush` → `os.fsync` → `close`.
6. `tmp.rename(service_dir / "_index.json")`.

### Требования к `State.find_success_run`

Новый метод `State`:

```python
def find_success_run(self, exporter: str, output_path: Path) -> Optional[tuple[str, str]]:
    """Return (started_at, finished_at) of the successful run that produced
    output_path, or None if no such row exists."""
```

Один `SELECT started_at, finished_at FROM runs WHERE exporter=? AND status='success' AND output_path=? ORDER BY id DESC LIMIT 1`. Индекс по `(exporter, status, output_path)` не нужен для MVP-scale; если начнёт тормозить, добавим.

### Точки отказа

- Поломка `service_dir.iterdir()` (permissions и т.п.) — логируем, оставляем старый `_index.json` как есть.
- Поломка `json.dumps` — не должно случаться (данные контролируемые), но логируем.
- Поломка `rename` — `._index.json.tmp` остаётся; следующий успешный run его перезапишет.
- Между `flush/fsync` и `rename` kill — `._index.json.tmp` остаётся, старый `_index.json` не тронут. На следующий run — перестройка.

### Что manifest writer **не делает**

- Не удаляет снапшоты из директории. Удаление — задача внешних инструментов.
- Не валидирует структуру снапшота (например, что JSON parseable). Это задача экспортёра и потребителя.
- Не обновляется лениво при `run-once` с `write_manifest=false`: флаг — единственный переключатель.

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

    def find_success_run(self, exporter: str, output_path: Path) -> Optional[tuple[str, str]]:
        """Для manifest writer: вернуть (started_at, finished_at) успешного
        run'а, породившего output_path, либо None."""
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
# Глобально включаем публичный манифест. Каждый экспортёр может
# перекрыть через свой write_manifest: true/false.
write_manifest: true

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

**`test_config.py`**: валидный YAML парсится, невалидный (дубликаты имён, неправильный mode, отсутствие extension при stdout) — падает с понятной ошибкой. Плюс: `write_manifest` парсится глобально и per-exporter; `effective_write_manifest` возвращает правильную комбинацию (None → inherit, True/False → override).

**`test_runner.py`**: прогон на фейковом экспортёре (баш-скрипт типа `echo '{"hello": "world"}'`), проверка что файл появился в правильном месте с правильным именем, проверка атомарности (при `exit 1` — tmp удалён, финального файла нет). Плюс: при `write_manifest=true` после run'а `_index.json` валиден и содержит снимок; при `false` — не создаётся; при `false` существующий `_index.json` **не** трогается (инвариант для включаемых/выключаемых источников).

**`test_storage.py`**: проверка генерации timestamp и путей.

**`test_manifest.py`**: (a) пустая директория → валидный JSON с `snapshots: []`; (b) смешанное содержимое (валидные snapshot'ы + посторонний `README.md` + скрытый `.foo.tmp` + старый `_index.json`) → в манифест попадают только валидные snapshot'ы, отсортированы asc; (c) directory-mode снапшот — `type=directory`, `size_bytes` = сумма файлов в поддереве; (d) снапшот, которому **нет** строки в state.db (ручной файл) — попадает в манифест без `run_*_at`, но с `size_bytes`; (e) atomicity: записать манифест, затем прервать между `fsync` и `rename` (симулируем через патч `Path.rename`) → старый `_index.json` не тронут, `._index.json.tmp` остаётся, следующий успешный вызов перезаписывает чисто; (f) `write_manifest=false` глобально, но `write_manifest=true` per-exporter → манифест только у этого экспортёра.

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
6. **Интеграция с потребителями** — ссылка на раздел «Публичный контракт для потребителей» (filesystem layout, манифест). Короткий пример karlicoss-совместимой интеграции: `my.<module>.export_path = './data/lastfm/*.json'`. Упоминание про манифест как альтернативу для потребителей, которым нужны метаданные run'а.

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
9. С `write_manifest: true` в конфиге — после `run-once lastfm` файл `/data/lastfm/_index.json` существует, валиден по схеме (см. «Manifest (`_index.json`)»), содержит ровно один снапшот с корректными `timestamp`/`path`/`type`/`size_bytes`/`run_started_at`/`run_ended_at`. Второй успешный run добавляет вторую запись, отсортированную asc
10. Если `write_manifest: true` и ручной файл `2020-01-01T00-00-00.json` положен в `/data/lastfm/`, следующий run включает его в манифест без `run_*_at`
11. Если перед `run-once` в `/data/lastfm/` лежал посторонний файл `README.md` — манифест его игнорирует
