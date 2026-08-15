import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// Testing Library only auto-registers cleanup when vitest runs with
// `globals: true`, which this project does not. Without this, every render
// stays mounted and leaks into the next test — queries then fail with
// "found multiple elements" rather than anything about the component.
afterEach(cleanup)
