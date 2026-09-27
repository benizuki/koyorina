import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import css from 'highlight.js/lib/languages/css'
import ini from 'highlight.js/lib/languages/ini'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import markdown from 'highlight.js/lib/languages/markdown'
import python from 'highlight.js/lib/languages/python'
import sql from 'highlight.js/lib/languages/sql'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'

// 生成されるのはこの範囲。全言語を読み込むと、使わない文法まで配ることになる。
for (const [name, language] of Object.entries({ bash, css, ini, javascript, json, markdown,
                                                python, sql, typescript, xml, yaml })) {
  hljs.registerLanguage(name, language)
}

const BY_SUFFIX: Record<string, string> = {
  py: 'python', ts: 'typescript', js: 'javascript', mjs: 'javascript', json: 'json',
  css: 'css', vue: 'xml', html: 'xml', md: 'markdown', yaml: 'yaml', yml: 'yaml',
  sql: 'sql', toml: 'ini', ini: 'ini', sh: 'bash',
}

/** 色付けしたHTML。文法が分からないものはそのまま文字として出す。 */
export function highlight(path: string, text: string): string {
  const suffix = path.slice(path.lastIndexOf('.') + 1).toLowerCase()
  const language = BY_SUFFIX[suffix]
  // highlight.js は入力をエスケープして返す。生成されたコードをそのまま描画しない。
  if (!language) return escape(text)
  try { return hljs.highlight(text, { language, ignoreIllegals: true }).value }
  catch { return escape(text) }
}

function escape(text: string): string {
  return text.replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;',
    '"': '&quot;', "'": '&#39;' }[character] as string))
}
