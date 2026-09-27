/** フォーク時に変える名前・画像はここへ集約する。
 *
 * 表示名は VITE_APP_NAME（frontend/.env）で上書きできる。画像は
 * @/assets/brand-wordmark.png・@/assets/brand-login-art.png を差し替え、
 * 小さいロゴマークは @/components/KoyoriLogo.vue をまるごと置き換える。
 */
export const APP_NAME = import.meta.env.VITE_APP_NAME || 'Koyorina'
