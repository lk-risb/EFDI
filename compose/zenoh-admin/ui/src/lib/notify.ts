import {toast} from 'sonner'
import {useNotifications} from '@/store/notifications'

export const notify = {
  success: (message: string) => {
    const text = message.trim() || 'Operation succeeded'
    toast.success(text)
    useNotifications.getState().push('success', text)
  },
  error: (message: string) => {
    const text = message.trim() || 'Operation failed'
    toast.error(text)
    useNotifications.getState().push('error', text)
  },
}
