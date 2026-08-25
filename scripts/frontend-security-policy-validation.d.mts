export interface RequiredPolicyOptions {
  readonly requireFrameAncestors?: boolean;
}

export function extractSingleCspMeta(html: string, label: string): string;

export function assertRequiredPolicy(
  policy: string | null | undefined,
  label: string,
  options?: RequiredPolicyOptions,
): Map<string, string[]>;

export function resolveDeployedFrontendTarget(
  rawValue: string,
  configuredApiOrigin: string | null,
): URL;

export function readBoundedHtml(response: Response, label: string): Promise<string>;
