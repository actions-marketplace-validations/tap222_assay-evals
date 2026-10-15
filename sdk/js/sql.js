"use strict";
/**
 * What a SQL statement does, in a few words: the operation, the table, the columns it sets and
 * the ones it leaves empty. Raw SQL changes with every id and timestamp; this doesn't, so a test's
 * writes can be compared with its last passing run.
 *
 *   summarize('INSERT INTO "bookings"("id", "amount") VALUES (DEFAULT, $1)', [null])
 *   // { op: "INSERT", table: "bookings", set: ["amount"], empty: ["id", "amount"] }
 *
 * Empty: DEFAULT, NULL, or a parameter bound to null. A column the statement doesn't name isn't
 * known here: that needs the table's schema. Not a SQL parser: anything it can't read is null.
 */

const MAX_COLUMNS = 100;
const OPS = { insert: "INSERT", update: "UPDATE", delete: "DELETE", select: "SELECT" };

/** sql with comments, string literals and quoted names blanked to spaces, so keywords can be
 * searched without matching inside them; with literals = false, only the comments. Same length as sql. */
function blank(sql, literals = true) {
  let out = "";
  for (let i = 0; i < sql.length; i++) {
    const c = sql[i], next = sql[i + 1];
    if (c === "-" && next === "-") {
      const end = sql.indexOf("\n", i);
      const stop = end < 0 ? sql.length : end;
      out += " ".repeat(stop - i);
      i = stop - 1;
    } else if (c === "/" && next === "*") {
      const end = sql.indexOf("*/", i + 2);
      const stop = end < 0 ? sql.length : end + 2;
      out += " ".repeat(stop - i);
      i = stop - 1;
    } else if (c === "'" || c === '"') {
      let j = i + 1;
      while (j < sql.length && !(sql[j] === c && sql[j + 1] !== c)) j += sql[j] === c ? 2 : 1;
      out += literals ? c + " ".repeat(Math.max(0, Math.min(j, sql.length) - i - 1)) + (j < sql.length ? c : "")
                      : sql.slice(i, j + 1);
      i = j;
    } else {
      out += c;
    }
  }
  return out;
}

/** Characters inside parentheses blanked too: what's left is the statement's own level. */
function topLevel(b) {
  let depth = 0, out = "";
  for (const c of b) {
    if (c === "(") depth++;
    out += depth > 0 ? " " : c;
    if (c === ")") depth = Math.max(0, depth - 1);
  }
  return out;
}

/** The index of the parenthesis that closes the one at `open`, in blanked text. */
function closing(b, open) {
  let depth = 0;
  for (let i = open; i < b.length; i++) {
    if (b[i] === "(") depth++;
    else if (b[i] === ")" && --depth === 0) return i;
  }
  return -1;
}

/** sql's text between start and end split on commas at that level. */
function split(sql, b, start, end) {
  const parts = [];
  let depth = 0, from = start;
  for (let i = start; i < end; i++) {
    if (b[i] === "(") depth++;
    else if (b[i] === ")") depth--;
    else if (b[i] === "," && depth === 0) {
      parts.push(sql.slice(from, i).trim());
      from = i + 1;
    }
  }
  parts.push(sql.slice(from, end).trim());
  return parts.filter((p) => p !== "");
}

/** A name without its quotes or schema: "public"."bookings" → bookings. */
function name(text) {
  const parts = String(text).trim().match(/"(?:[^"]|"")*"|[^."\s]+/g) || [];
  const last = parts[parts.length - 1] || "";
  return last.startsWith('"') ? last.slice(1, -1).replace(/""/g, '"') : last.toLowerCase(); // Postgres folds unquoted names
}

/** The table name that starts at `at` in sql (quoted or not), and where it ends. */
function tableAt(sql, b, at) {
  const m = /^\s*((?:"(?:[^"]|"")*"|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:"(?:[^"]|"")*"|[A-Za-z_][\w$]*))*)/.exec(sql.slice(at));
  return m ? { table: name(m[1]), end: at + m[0].length } : null;
}

function isEmpty(value, values) {
  const v = value.trim().toLowerCase();
  if (v === "default" || v === "null" || v.startsWith("null::")) return true;
  const p = /^\$(\d+)(\s*::.*)?$/.exec(v);
  if (p && Array.isArray(values)) {
    const bound = values[Number(p[1]) - 1];
    return bound === null || bound === undefined;
  }
  return false;
}

function cap(list) {
  return list.slice(0, MAX_COLUMNS);
}

function insert(sql, b, top, at, values) {
  const t = tableAt(sql, b, at);
  if (!t) return null;
  const open = b.indexOf("(", t.end);
  const valuesAt = top.slice(t.end).search(/\bvalues\b/i);
  const selectAt = top.slice(t.end).search(/\bselect\b/i);
  const firstKeyword = Math.min(...[valuesAt, selectAt].filter((x) => x >= 0).map((x) => x + t.end), Infinity);
  if (open < 0 || open > firstKeyword) return { op: "INSERT", table: t.table, set: [], empty: [] }; // DEFAULT VALUES
  const close = closing(b, open);
  if (close < 0) return null;
  const columns = split(sql, b, open + 1, close).map(name);
  if (valuesAt < 0) return { op: "INSERT", table: t.table, set: cap(columns), empty: [] }; // INSERT ... SELECT
  // Each row's values; a column is empty when every row leaves it empty.
  const empty = new Set(columns);
  let from = t.end + valuesAt + "values".length, rows = 0;
  for (;;) {
    const m = /^\s*,?\s*\(/.exec(b.slice(from));
    if (!m) break;
    const rowOpen = from + m[0].length - 1, rowClose = closing(b, rowOpen);
    if (rowClose < 0) break;
    const row = split(sql, b, rowOpen + 1, rowClose);
    columns.forEach((c, i) => { if (row[i] === undefined || !isEmpty(row[i], values)) empty.delete(c); });
    rows++;
    from = rowClose + 1;
  }
  if (!rows) empty.clear();
  return { op: "INSERT", table: t.table, set: cap(columns), empty: cap(columns.filter((c) => empty.has(c))) };
}

function update(sql, b, top, at, values) {
  const t = tableAt(sql, b, at);
  if (!t) return null;
  const setAt = top.slice(t.end).search(/\bset\b/i);
  if (setAt < 0) return null;
  const start = t.end + setAt + 3;
  const stop = top.slice(start).search(/\b(where|from|returning)\b/i);
  const end = stop < 0 ? sql.length : start + stop;
  const set = [], empty = [];
  for (const pair of split(sql, b, start, end)) {
    const eq = blank(pair).indexOf("=");
    if (eq < 0 || pair.trim().startsWith("(")) continue; // (a, b) = (...): not worth reading
    const column = name(pair.slice(0, eq));
    set.push(column);
    if (isEmpty(pair.slice(eq + 1), values)) empty.push(column);
  }
  return { op: "UPDATE", table: t.table, set: cap(set), empty: cap(empty) };
}

/** { op, table, set, empty } for an INSERT, UPDATE, DELETE or SELECT; null for anything else
 * (BEGIN, SET, a SELECT with no table) or what can't be read. */
function summarize(sql, values) {
  if (typeof sql !== "string" || !sql.trim()) return null;
  try {
    sql = blank(sql, false);
    const b = blank(sql);
    const top = topLevel(b);
    // After a WITH, the statement is the first INSERT, UPDATE, DELETE or SELECT at the top level.
    const m = /\b(insert\s+into|update|delete\s+from|select)\b/i.exec(top);
    if (!m) return null;
    const op = OPS[m[1].split(/\s/)[0].toLowerCase()];
    const at = m.index + m[0].length;
    if (op === "INSERT") return insert(sql, b, top, at, values);
    if (op === "UPDATE") return update(sql, b, top, at, values);
    if (op === "DELETE") {
      const t = tableAt(sql, b, at);
      return t && { op, table: t.table, set: [], empty: [] };
    }
    const from = top.slice(at).search(/\bfrom\b/i);
    if (from < 0) return null;
    const t = tableAt(sql, b, at + from + 4);
    return t && { op, table: t.table, set: [], empty: [] };
  } catch {
    return null; // never break the query over a summary
  }
}

module.exports = { summarize };
