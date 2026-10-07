# Кампании и аккаунты

## Кампания

Файл `config/campaigns/<id>.yaml`. Если `id` не указан, берётся имя файла. Загружается при старте, валидируется Pydantic (`schemas.Campaign`); ошибка указывает файл.

| Поле | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `id` | str | имя файла | идентификатор для `--campaign` |
| `name` | str | — | название |
| `rate_per_1k_views` | float ≥ 0 | — | ставка за 1000 просмотров |
| `platforms` | list | — | `youtube`, `tiktok`, `instagram` — для каких платформ генерировать метаданные |
| `clip_min_sec` / `clip_max_sec` | float | 20 / 60 | границы длительности клипа |
| `clip_count` | int | 3 | сколько клипов рендерить |
| `language` | str\|null | null | язык Whisper; null — автоопределение |
| `must_include_tags` | list | [] | хэштеги, которые обязательно добавляются |
| `mentions` | list | [] | упоминания, дописываются в описание |
| `forbidden` | list | [] | слова, которых не должно быть в метаданных |
| `notes` | str | "" | пожелания для LLM при выборе моментов |
| `accounts` | list | [] | id аккаунтов для публикации |
| `selection` | enum | `transcript` | как искать моменты: `transcript` — LLM по речи (подкасты); `signals` — пики звука + YouTube «Most replayed» + чат, без LLM (игры; ADR-0014); `hybrid` — LLM по речи, пики подсказываются в промпте и дают 30% оценки (говорящие стримеры; ADR-0017) |
| `layout` | enum | `crop` | `crop` — кроп 9:16 по лицу/центру; `fit_blur` — весь кадр по центру, сверху и снизу размытый фон (геймплей; ADR-0015) |
| `fit_zoom` | float 1–2 | 1.0 | для `fit_blur`: >1 — кадр крупнее, края срезаются |
| `title_overlay` | bool | false | заголовок клипа (YouTube-заголовок от LLM, без хэштегов и эмодзи) крупно сверху кадра; при `fit_blur` — в верхней размытой полосе. Правка заголовка в ревью видео не меняет |
| `transcribe` | bool | true | `false` — без Whisper и субтитров (только при `selection: signals`) |

```yaml
id: example
name: Example podcast clips
rate_per_1k_views: 1.5
platforms: [youtube, tiktok]
clip_min_sec: 20
clip_max_sec: 60
clip_count: 3
language: ru
must_include_tags: ["#shorts"]
mentions: ["@example_brand"]
forbidden: ["казино"]
notes: Фокус на практических советах.
accounts: [yt_main, tt_main]
```

Игровая кампания — `config/campaigns/cs2.yaml`: `selection: signals`, `transcribe: false`, `layout: fit_blur`, клипы 15–45 с (окно ~22 с, пик на 65% клипа). LLM в такой кампании пишет только заголовки и описания.

Доход: `views / 1000 × rate_per_1k_views` по последнему снимку статистики каждой публикации (`cf report`). Итог не хранится — пересчитывается из истории.

## Аккаунт

Файл `config/accounts.yaml` (шаблон `config/accounts.example.yaml`; если основного нет — читается шаблон).

| Поле | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `id` | str | — | идентификатор |
| `platform` | enum | — | `youtube`, `tiktok`, `instagram` |
| `name` | str | — | человекочитаемое имя |
| `token_ref` | str\|null | null | имя файла токена в `data/secrets/` (не сам токен!) |
| `daily_limit` | int | 3 | максимум публикаций в календарные сутки по `timezone` |
| `posting_windows` | list | `["10:00-22:00"]` | окна публикации `HH:MM-HH:MM` в `timezone` |
| `timezone` | str | UTC | IANA-зона, например `Europe/Moscow` |

Токены в YAML не хранятся. `token_ref` — только идентификатор `[A-Za-z0-9_.-]+`.
