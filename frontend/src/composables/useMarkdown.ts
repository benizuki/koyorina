import { Marked } from 'marked'
import { highlight } from './useHighlight'

/** 生成物のMarkdownを読む形にする。中身は生成されたもので、信用しない。
 *
 * 素のmarkedはHTMLもjavascript:のリンクもそのまま通す。ここで両方を塞ぐ。
 * 画面のCSPは script-src 'self' なので、仮に属性が混ざっても実行はされないが、
 * そこに頼らず、出すHTMLはこちらが組み立てたものだけにする。
 */
const renderer = {
  html(token: { text: string }) { return escape(token.text) },
  link(token: { href: string; title?: string | null; text: string }) {
    const href = safeHref(token.href)
    if (!href) return escape(token.text)
    // 外部のページは別のタブで開く。開いた先から元の画面を触らせない。
    return `<a href="${escape(href)}" target="_blank" rel="noopener noreferrer">${escape(token.text)}</a>`
  },
  image(token: { href: string; text: string }) {
    const href = safeHref(token.href)
    return href ? `<img src="${escape(href)}" alt="${escape(token.text)}" />` : escape(token.text)
  },
  code(token: { text: string; lang?: string }) {
    const name = (token.lang ?? '').split(/\s+/)[0]
    // 言語の指定を拡張子に見立てて、ソース表示と同じ色付けを使う。
    const body = name ? highlight(`x.${name}`, token.text) : escape(token.text)
    return `<pre><code>${body}</code></pre>`
  },
}

const markdown = new Marked({ gfm: true, breaks: false })
markdown.use({ renderer })

export function renderMarkdown(text: string): string {
  return markdown.parse(text, { async: false }) as string
}

function safeHref(href: string): string {
  const value = (href ?? '').trim()
  // javascript: や data: は開かせない。ページ内・http(s)・メールだけ通す。
  return /^(https?:\/\/|mailto:|#|\/|\.{0,2}\/)/i.test(value) ? value : ''
}

function escape(text: string): string {
  return String(text).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;',
    '"': '&quot;', "'": '&#39;' }[character] as string))
}
