import { useState, type InputHTMLAttributes } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import { cn } from '@/lib/utils'

type PasswordInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>

export function PasswordInput({ className, disabled, ...props }: PasswordInputProps) {
  const [revealed, setRevealed] = useState(false)
  const label = revealed ? 'Hide password' : 'Show password'
  return <div className="relative w-full">
    <input {...props} type={revealed ? 'text' : 'password'} disabled={disabled} className={cn(className, 'pr-10')} />
    <button type="button" onClick={() => setRevealed((value) => !value)} disabled={disabled} aria-label={label} aria-pressed={revealed} title={label} className="absolute inset-y-0 right-0 flex w-10 items-center justify-center rounded-r-md text-zinc-500 transition-colors hover:text-zinc-900 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-accent-ring disabled:cursor-not-allowed disabled:opacity-50 dark:text-zinc-400 dark:hover:text-white">
      {revealed ? <EyeOff size={16} aria-hidden="true" /> : <Eye size={16} aria-hidden="true" />}
    </button>
  </div>
}
