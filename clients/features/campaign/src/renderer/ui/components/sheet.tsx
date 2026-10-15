import * as DialogPrimitive from '@radix-ui/react-dialog';
import type * as React from 'react';
import {cn} from './utils.js';

function Sheet(props: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="sheet" {...props} />;
}

function SheetTrigger(props: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  return <DialogPrimitive.Trigger data-slot="sheet-trigger" {...props} />;
}

function SheetClose(props: React.ComponentProps<typeof DialogPrimitive.Close>) {
  return <DialogPrimitive.Close data-slot="sheet-close" {...props} />;
}

function SheetContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content>) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay
        data-slot="sheet-overlay"
        className="sheet-overlay campaign-overlay"
      />
      <DialogPrimitive.Content
        data-slot="sheet-content"
        className={cn('sheet-content campaign-overlay', className)}
        aria-describedby={undefined}
        {...props}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

function SheetTitle(props: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return <DialogPrimitive.Title data-slot="sheet-title" {...props} />;
}

export {Sheet, SheetClose, SheetContent, SheetTitle, SheetTrigger};
