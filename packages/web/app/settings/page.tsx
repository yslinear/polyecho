/**
 * /settings — Global configuration
 * Phase 0 skeleton: settings key/value editor and template management come in Phase 4.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export default function SettingsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Settings</h1>
        <p className="text-muted-foreground mt-1">
          Global trading config and copy templates
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Copy Templates</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              Conservative / Balanced / Aggressive template editor will appear
              here. Changes write to copy_templates via Server Action.
            </p>
            <Badge variant="outline" className="mt-3">
              Phase 4
            </Badge>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Risk Parameters</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              Global pause toggle, conservative mode, daily loss limits, and
              category whitelist editor will appear here.
            </p>
            <Badge variant="outline" className="mt-3">
              Phase 4
            </Badge>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Web Push Subscriptions</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Push subscription management and notification preferences will
            appear here. Requires Safari 16.4+ and adding Dashboard to Home
            Screen as PWA.
          </p>
          <Badge variant="outline" className="mt-3">
            Phase 5
          </Badge>
        </CardContent>
      </Card>
    </div>
  );
}
