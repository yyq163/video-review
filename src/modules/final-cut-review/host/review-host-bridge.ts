import type { EntryMode } from '../contracts/types';
import type { PrincipalAuthorizationPort, ReviewHostBridge } from '../ports';
import { sanitizeDownloadFileName } from '../core/file-names';

export class BrowserReviewHostBridge implements ReviewHostBridge {
  constructor(
    public readonly entryMode: EntryMode,
    private readonly authorization?: PrincipalAuthorizationPort,
  ) {}

  getAuthorizationAdapter(): PrincipalAuthorizationPort | undefined {
    return this.authorization;
  }

  notify(message: string): void {
    window.dispatchEvent(new CustomEvent('fj-review:notify', { detail: { message } }));
  }

  downloadBlob(blob: Blob, fileName: string): void {
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = href;
    anchor.download = sanitizeDownloadFileName(fileName);
    anchor.rel = 'noopener';
    anchor.style.display = 'none';
    document.body.appendChild(anchor);
    anchor.click();
    window.setTimeout(() => {
      anchor.remove();
      URL.revokeObjectURL(href);
    }, 60_000);
  }

  downloadUrl(url: string, fileName: string): void {
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = sanitizeDownloadFileName(fileName);
    anchor.rel = 'noopener';
    anchor.style.display = 'none';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }
}
