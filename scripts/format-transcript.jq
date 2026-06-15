# Pretty-prints the Claude Code Agent tool's JSONL transcript for human
# operator monitoring. One event per line. Malformed lines never abort.

def trunc($n): if (length // 0) > $n then .[:$n] + "…" else . end;

def basename: split("/") | last;

def first_nonempty_line:
  split("\n") | map(select(length > 0)) | (.[0] // "");

def flatten_result_content:
  if   type == "string" then .
  elif type == "array"  then
      map(
        if   type == "object" and .type == "text" then (.text // "")
        elif type == "object" then (.content // "")
        else tostring
        end
      ) | join("\n")
  else tostring
  end;

def summarize_tool_input($name; $input):
  ($input // {}) as $a
  | if   $name == "Bash" then
      (($a.command // "") | gsub("\n"; " ") | trunc(80))
    elif $name == "Read" then
      ($a.file_path // "" | basename)
    elif $name == "Edit" then
      ($a.file_path // "" | basename) + ", " + ((($a.new_string // "") | length) | tostring) + " bytes"
    elif $name == "Write" then
      ($a.file_path // "" | basename) + ", " + ((($a.content // "") | length) | tostring) + " bytes"
    elif $name == "Grep" then
      ($a.pattern // "") + (if $a.path then ", " + $a.path else "" end)
    elif $name == "Glob" then
      ($a.pattern // "")
    elif $name == "Agent" then
      ($a.subagent_type // "?") + ": " + (($a.description // "") | trunc(60))
    else
      ($a | keys_unsorted | join(","))
    end;

def render_tool_use:
  "🔧 " + (.name // "?") + "(" + summarize_tool_input(.name // ""; .input // {}) + ")";

def render_tool_result:
  (.is_error // false) as $err
  | (.content | flatten_result_content | first_nonempty_line | trunc(200)) as $line
  | if $err then "❌ <tool> → " + $line
    else "✅ <tool> → " + $line
    end;

def render_assistant_content:
  .content // []
  | .[]
  | if   .type == "text" then
      "💬 " + ((.text // "") | gsub("\n"; " ") | trunc(200))
    elif .type == "tool_use" then
      render_tool_use
    else empty
    end;

def render_user_content:
  .content // []
  | .[]
  | if   (type == "object" and .type == "tool_result") then render_tool_result
    else empty
    end;

def render_event:
  . as $e
  | if   $e.type == "assistant" then $e.message | render_assistant_content
    elif $e.type == "user"      then $e.message | render_user_content
    elif $e.type == "result"    then "📤 " + (($e.result // "") | gsub("\n"; " ") | trunc(200))
    else empty
    end;

try render_event catch "⚠️  malformed line skipped"
