import { defineStore } from 'pinia'
import { useNotification } from '../composables/useNotification'

export const useNotificationStore = defineStore('notification', () => {
  const { prefs, permission, requestPermission, loadPrefs, savePrefs, onJobStatusChange } = useNotification()
  return { prefs, permission, requestPermission, loadPrefs, savePrefs, onJobStatusChange }
})
