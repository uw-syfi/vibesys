import type {ButtonHTMLAttributes, JSX} from 'react';

/** How a button reads: the default, the one action a surface leads with, or inline text. */
export type ButtonVariant = 'default' | 'primary' | 'link';

/**
 * A button that does not submit a form unless `type` says so: the default `type` is `button`, the
 * opposite of HTML's. Styled by `variant`, plus any `className` the caller adds.
 */
export function Button(
  props: {readonly variant?: ButtonVariant} & ButtonHTMLAttributes<HTMLButtonElement>,
): JSX.Element {
  const {variant = 'default', className, type = 'button', ...button} = props;
  const classes = [variant === 'default' ? '' : variant, className ?? ''].filter(Boolean);
  return (
    <button
      {...button}
      type={type}
      className={classes.length === 0 ? undefined : classes.join(' ')}
    />
  );
}
