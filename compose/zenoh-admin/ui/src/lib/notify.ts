import { toast } from 'sonner'
import { useNotifications } from '@/store/notifications'

export const notify = {
  success: (message: string) => {
    toast.success(message)
    useNotifications.getState().push('success', message)
  },
  error: (message: string) => {
    toast.error(message)
    useNotifications.getState().push('error', message)
  },
}
