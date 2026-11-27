# Building a Cliquet app

A Cliquet app keeps a small dataset under rules. It is one Python file, `app.py`: actions change the data, invariants say what must always hold, queries answer questions, and views lay out a screen. Every change runs on a copy of the data. Then every invariant runs on the result, and the change is kept or refused with the reasons. Agents use the app as MCP tools. Its screens give people a quick look, often inside their assistant.

You build it with these tools, on a draft. Nothing is live until `publish_app`.

## The steps

- [ ] 1. Agree with the person on what the app keeps, who uses it, and the rules that must always hold, and ask their time zone, for the app's dates. If they want screens, ask what each one is for. Ask when it isn't clear.
- [ ] 2. `create_app(app)` starts a draft from a small template. `read_example(example)` shows an example file by file: read the one closest to the app, from "The examples" below, for patterns, and write only what the person asked for. When the person has data of their own, get it now: see "First data" below.
- [ ] 3. Read `read_guide("app")`. Write `app.py` with `write_file` or `edit_file`. Give the app an accent and an icon, as the app guide says, even without a screen: they mark its tile wherever people pick it. Choose the icon with `find_icons`, by a word for the app's subject.
- [ ] 4. `check_app(app)`. Fix what it reports, and call it again until it passes.
- [ ] 5. Read `read_guide("tests")`. Write `tests/test_<name>.py`. Every invariant must refuse at least once in the tests.
- [ ] 6. `run_tests(app)`. Fix and run again until they pass.
- [ ] 7. `start_preview(app)` runs the draft on a copy of the current data: the draft's `data/` files before the first publish, the live data after. Start one before trying anything. Every tool that tries something works on it: `try_call` calls one action or query, `try_view` clicks through a view, `take_screenshots` takes pictures of the views, and `show_preview` shows a view to the person. What any of them changes, for any role, is kept in the preview for the next try, and never reaches the live data. Changing the code keeps the preview's data. Call `start_preview` again to start over from the current data, and `stop_preview` when you are done. The person's own tools, `read_app_data`, `write_app_data` and `show_app_view`, use the live app's data, never the preview's. The preview's link opens it in the person's browser.
- [ ] 8. If the person asked for a screen: read `read_guide("views")`, then write the view. Follow its "Design rules". Use `find_classes` to look up a component's classes. Look at it with `take_screenshots(app, role)` at both widths, for each role whose screen differs, and once with `theme="dark"`, click through it with `try_view`, and show it with `show_preview` before publishing. Fix what looks or works wrong. If the client can't draw views, ask the person to open the preview's link.
- [ ] 9. Read `read_guide("instructions")`. Write `instructions.md`.
- [ ] 10. Read `read_guide("publishing")`. Call `check_app(app)` and show the person its `changes` and `data`, in your own words. Before the first publish, also tell them what its first data will be: see "First data" below. When they agree, call `publish_app(app, remark)`. It runs every check again, the tests included, and publishes only if they all pass.
- [ ] 11. Give the person the app's web page for each role, from the links in `publish_app`'s answer: `read_app_overview(app)` gives them again later. Say how to use it from an assistant: see "Who can use the app" in `read_guide("publishing")`. This connection reaches it already: `list_apps` lists it, and `describe_app` describes it.

## The examples

Each example stands for a kind of app. Read the one whose kind is closest, even when its subject differs.

| Example | The kind | Apps it fits | Copy from it |
| --- | --- | --- | --- |
| `meeting-rooms` | A plan on a calendar | Room or equipment bookings, a rota, leave, renewals | Rules over time that the invariants, the actions and a query share; a query that says why each option can't work; a move that keeps a record's number; a week, a day and a search on screen |
| `environments` | A register that every agent looks up | A service catalog, a register of contracts or vendors, feature flags | Records with a name and references that must exist; a role that only reads; verdicts with their reasons; a rule against secrets in free text; instructions that say when to look |
| `ledger` | A file in a domain format, with its library | A household's accounts, an envelope budget, any plain-text format | The library's check as an invariant; an import that reconciles, skips what is there and freezes what a statement confirmed; settings files; a query of what to do next; charts and data grids |

## First data

Until the first publish, the draft may hold the app's first data, as `data/<file>` for each file the app declares, by its name or by a pattern. The first publish makes these files the app's data, which everyone the person shares the app with sees. After it, data changes only through the app's actions, or through a migration.

- For a new app, write made-up data, so that its screens aren't empty, then call `start_preview` again.
- Before the first publish, tell the person that the draft's `data/` files become the app's first data, seen by everyone they share it with. Ask whether to keep the made-up files, or to remove them with `delete_file` so the app starts empty.
- Before the person gives you data of their own, tell them who can read it: see "Who can use the app" in `read_guide("publishing")`.

When the person has data of their own, such as a ledger or a spreadsheet, get it before you write the app:

1. Call `ask_for_file(app, "data/<file>")`. When you show views in the conversation, the person uploads the file there, from a panel, which sends you a message with the file's path and size once it is in, or asks the person to tell you. Otherwise give them its link, which works either way: they upload the file from their browser. It goes straight into the draft. Never retype their data with `write_file`.
2. When they say it is there, or the panel tells you, call `list_files(app)`, and look at the file's start with `read_file`. It reads 1,000 lines by default, and 2,000 at most. It returns an object: `text` holds lines `from` to `to`, out of `lines`, and `next` says how to read more.
3. Write the app to fit the data. Then let the app read it: `check_app(app)` runs the rules on it, and `start_preview(app)` and `try_call` ask the app's queries about it. Don't read the whole file through `read_file`: write a query for what you want to know.

## Changing an app that is live

The same steps, from step 3. The draft starts as a copy of the live version: `list_files(app)` shows it, `read_file` reads a file's lines, and `delete_file` removes one. `discard_draft(app)` throws away what isn't published, and goes back to the live version.

When the person says the live app failed, or gives you an error id such as `e-1a2b3c4d5e6f`, read the error first with `read_error(app, error)`: see "When the live app fails" in `read_guide("publishing")`.

When the person wants an earlier version back, call `restore_version(app, version)`, try it, then publish it: see "Restoring an earlier version" in `read_guide("publishing")`.

## Rules

- Don't publish before `check_app` and `run_tests` pass, and before the person saw what `check_app` says changes.
- Don't say a view looks right unless someone looked at it: see "Looking at it" in `read_guide("views")`.
- Don't stop at an error. Each one says what was wrong and what to call next.
- If the request needs something Cliquet can't do yet, stop and say so: see the end of `read_guide("app")`.
