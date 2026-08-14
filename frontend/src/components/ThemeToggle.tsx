import { Monitor, Moon, Sun } from 'lucide-react'
import { useTheme, type ThemeChoice } from '../lib/theme'

/**
 * Dark / light / system, as three explicit choices rather than a switch.
 *
 * A two-state toggle has to decide what "off" means on a machine that follows
 * the OS, and every answer is wrong for somebody. Three buttons say exactly
 * what will happen, and "system" stays live — it keeps following if the OS
 * changes at sunset.
 */
const OPTIONS: { value: ThemeChoice; icon: typeof Sun; label: string }[] = [
  { value: 'light', icon: Sun, label: 'Light' },
  { value: 'dark', icon: Moon, label: 'Dark' },
  { value: 'system', icon: Monitor, label: 'Match system' },
]

export function ThemeToggle() {
  const [choice, , setChoice] = useTheme()

  return (
    <div
      role="radiogroup"
      aria-label="Colour theme"
      className="inline-flex items-center rounded border border-border p-0.5"
    >
      {OPTIONS.map(({ value, icon: Icon, label }) => {
        const active = choice === value
        return (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={active}
            title={label}
            aria-label={label}
            onClick={() => setChoice(value)}
            className={[
              'flex h-6 w-7 items-center justify-center rounded-sm transition-colors',
              active
                ? 'bg-accent-quiet text-accent-text'
                : 'text-text-subtle hover:text-text',
            ].join(' ')}
          >
            <Icon className="h-3.5 w-3.5" />
          </button>
        )
      })}
    </div>
  )
}
