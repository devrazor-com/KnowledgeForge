# Validation Contract — the four operations

JSON schemas define the messages. These four operations define how they move.
Deliberately unspecified: whether these are method calls, HTTP endpoints, or
anything else. In development they can be in-process; in deployment they will
be over the network. Neither changes the messages.

| Operation | Input | Output | Behaviour |
|---|---|---|---|
| start   | ValidationRequest        | run_id                    | Validates and accepts, returning promptly. Does not wait for the work. May instead reject with a reason, in which case no run is created. |
| events  | run_id, since_sequence   | stream of ExecutionEvent  | Events in order from after since_sequence. Pass 0 for everything. Stream ends after the terminal event. |
| result  | run_id                   | ValidationResult or none  | Returns nothing until the run reaches a terminal state. |
| cancel  | run_id                   | acknowledgement           | Ends the run with a cancelled event and a cancelled result. |

## Validation

Each module validates every contract message at its boundary:

- Module 1 validates requests before sending, and events and results when received.
- Module 3 validates requests when received, and events and results before sending.

Nothing else catches drift, since neither module imports the other's code.
Validating on the way in matters as much as on the way out: it stops either
side accepting invalid input silently and failing somewhere confusing later.

Schema `$id` values are bare filenames and `$ref` between schemas is relative,
so any validator that loads the four files from this folder resolves them with
no URL-to-file mapping.
