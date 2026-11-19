# zakup.sk.kz parser

## Запуск

- **`run.bat`** — ставит зависимости (Playwright + Chromium) и запускает полный сбор в `zakup_all`. Нужен Python 3.10+.
- `current.bat` — только текущие (не завершённые) объявления и реестры, в `zakup_current`.

## Разделы (`--sections`, по умолчанию все)

| Раздел | Что | Файлы |
|---|---|---|
| `open` | реестры жалоб, участников, поставщиков, ПКО/ДПО, категории, регламенты, выгрузки планов, справочники | `complaints_*`, `companies`, `*_suppliers`, `ppk_items`, `regulations`, `plan_extracts`, `dictionaries`… |
| `adverts` | объявления всех статусов + все лоты | `adverts_list`, `adverts_full` |
| `contracts` | договоры + карточки (акты, отчёты, допсоглашения) | `contracts_list`, `contracts_full` |
| `plans` | пункты планов (вкладка «Поиск планов», текущий год) | `plans_list`, `plans_full` |
| `files` | все документы из собранных данных | `files/`, `files_index.jsonl` |

`*_list` / прочие — `.jsonl`; `*_full` — сжатые части `.jsonl.gz` (читаются `pd.read_json(path, lines=True)`). После полного завершения собираются массивы без дублей `*.json` / `*.json.gz`.

## Полезные флаги

```
--sections open,adverts      только выбранные разделы
--advert-status CURRENT      только текущие объявления (или PUBLISHED,CLOSED,...)
--workers 8                  вкладок браузера параллельно
--min-free-gb 30             стоп скачивания файлов при малом месте на диске
--no-advert-details          без карточек (аналогично --no-contract/plan/complaint-details)
--show                       показывать окна браузера
--compact                    пересжать старые несжатые *_full.jsonl и выйти
--export-only                только собрать *.json из скачанного и выйти
```
