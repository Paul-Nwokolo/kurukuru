import { useCallback } from 'react'
import { navigate } from '../lib/router'

interface LinkProps extends React.AnchorHTMLAttributes<HTMLAnchorElement> {
  to: string
}

/**
 * An anchor that navigates in-app on a plain click.
 *
 * Modified clicks (ctrl/cmd/shift/middle) fall through to the browser, because
 * "open this instance in a new tab" is a thing people do and swallowing it
 * would be worse than having no links at all.
 */
export function Link({ to, onClick, children, ...rest }: LinkProps) {
  const handle = useCallback(
    (e: React.MouseEvent<HTMLAnchorElement>) => {
      onClick?.(e)
      if (e.defaultPrevented) return
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return
      e.preventDefault()
      navigate(to)
    },
    [to, onClick],
  )
  return (
    <a href={to} onClick={handle} {...rest}>
      {children}
    </a>
  )
}
