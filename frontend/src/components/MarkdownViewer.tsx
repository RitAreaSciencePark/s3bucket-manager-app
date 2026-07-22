/**
 * MarkdownViewer — full-featured Markdown renderer.
 *
 * Supports: headings, paragraphs, code blocks (syntax-highlighted),
 * inline code, tables, task lists, blockquotes, images, links,
 * strikethrough, ordered/unordered lists, horizontal rules,
 * and Mermaid diagrams (fenced code blocks with language "mermaid").
 */

import { useEffect, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github.css'

interface MarkdownViewerProps {
  content: string
  className?: string
}

function MermaidBlock({ code }: { code: string }) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!ref.current) return
    let cancelled = false
    import('mermaid').then(({ default: mermaid }) => {
      if (cancelled) return
      mermaid.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'loose' })
      const id = 'mmd-' + Math.random().toString(36).slice(2)
      mermaid
        .render(id, code)
        .then(({ svg }) => {
          if (!cancelled && ref.current) ref.current.innerHTML = svg
        })
        .catch(() => {
          if (!cancelled && ref.current) {
            ref.current.textContent = code
            ref.current.style.color = '#dc2626'
          }
        })
    })
    return () => { cancelled = true }
  }, [code])

  return <div ref={ref} className="md-mermaid" />
}

export default function MarkdownViewer({ content, className }: MarkdownViewerProps) {
  return (
    <div className={`md-body${className ? ' ' + className : ''}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          code({ className: cls, children, ...props }) {
            const match = /language-(\w+)/.exec(cls || '')
            const lang = match?.[1]
            const codeText = String(children).replace(/\n$/, '')

            if (lang === 'mermaid') {
              return <MermaidBlock code={codeText} />
            }

            // Inline code (no class): render as <code>
            if (!cls) {
              return <code className="md-inline-code" {...props}>{children}</code>
            }

            // Block code: rehype-highlight has already applied syntax coloring;
            // just wrap with our styled pre/code.
            return (
              <code className={cls} {...props}>
                {children}
              </code>
            )
          },
          a({ href, children, ...props }) {
            const isExternal = href && (href.startsWith('http://') || href.startsWith('https://'))
            return (
              <a
                href={href}
                {...(isExternal ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
                {...props}
              >
                {children}
              </a>
            )
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
