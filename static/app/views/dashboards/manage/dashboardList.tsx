import {Fragment} from 'react';
import styled from '@emotion/styled';
import type {Location, Query} from 'history';

import {
  createDashboard,
  deleteDashboard,
  fetchDashboard,
} from 'sentry/actionCreators/dashboards';
import {addErrorMessage, addSuccessMessage} from 'sentry/actionCreators/indicator';
import type {Client} from 'sentry/api';
import {Button} from 'sentry/components/button';
import {openConfirmModal} from 'sentry/components/confirm';
import type {MenuItemProps} from 'sentry/components/dropdownMenu';
import {DropdownMenu} from 'sentry/components/dropdownMenu';
import EmptyStateWarning from 'sentry/components/emptyStateWarning';
import Pagination from 'sentry/components/pagination';
import TimeSince from 'sentry/components/timeSince';
import {IconEllipsis} from 'sentry/icons';
import {t, tn} from 'sentry/locale';
import {space} from 'sentry/styles/space';
import type {Organization} from 'sentry/types/organization';
import {trackAnalytics} from 'sentry/utils/analytics';
import {useNavigate} from 'sentry/utils/useNavigate';
import withApi from 'sentry/utils/withApi';
import type {DashboardListItem} from 'sentry/views/dashboards/types';

import {cloneDashboard} from '../utils';

import DashboardCard from './dashboardCard';
import GridPreview from './gridPreview';

type Props = {
  api: Client;
  dashboards: DashboardListItem[] | undefined;
  location: Location;
  onDashboardsChange: () => void;
  organization: Organization;
  pageLinks: string;
};

function DashboardList({
  api,
  organization,
  location,
  dashboards,
  pageLinks,
  onDashboardsChange,
}: Props) {
  const navigate = useNavigate();
  function handleDelete(dashboard: DashboardListItem) {
    deleteDashboard(api, organization.slug, dashboard.id)
      .then(() => {
        trackAnalytics('dashboards_manage.delete', {
          organization,
          dashboard_id: parseInt(dashboard.id, 10),
        });
        onDashboardsChange();
        addSuccessMessage(t('Dashboard deleted'));
      })
      .catch(() => {
        addErrorMessage(t('Error deleting Dashboard'));
      });
  }

  async function handleDuplicate(dashboard: DashboardListItem) {
    try {
      const dashboardDetail = await fetchDashboard(api, organization.slug, dashboard.id);
      const newDashboard = cloneDashboard(dashboardDetail);
      newDashboard.widgets.map(widget => (widget.id = undefined));
      await createDashboard(api, organization.slug, newDashboard, true);
      trackAnalytics('dashboards_manage.duplicate', {
        organization,
        dashboard_id: parseInt(dashboard.id, 10),
      });
      onDashboardsChange();
      addSuccessMessage(t('Dashboard duplicated'));
    } catch (e) {
      addErrorMessage(t('Error duplicating Dashboard'));
    }
  }

  function renderDropdownMenu(dashboard: DashboardListItem) {
    const menuItems: MenuItemProps[] = [
      {
        key: 'dashboard-duplicate',
        label: t('Duplicate'),
        onAction: () => handleDuplicate(dashboard),
      },
      {
        key: 'dashboard-delete',
        label: t('Delete'),
        priority: 'danger',
        onAction: () => {
          openConfirmModal({
            message: t('Are you sure you want to delete this dashboard?'),
            priority: 'danger',
            onConfirm: () => handleDelete(dashboard),
          });
        },
      },
    ];

    return (
      <DropdownMenu
        items={menuItems}
        trigger={triggerProps => (
          <DropdownTrigger
            {...triggerProps}
            aria-label={t('Dashboard actions')}
            size="xs"
            borderless
            onClick={e => {
              e.stopPropagation();
              e.preventDefault();

              triggerProps.onClick?.(e);
            }}
            icon={<IconEllipsis direction="down" size="sm" />}
          />
        )}
        position="bottom-end"
        disabledKeys={dashboards && dashboards.length <= 1 ? ['dashboard-delete'] : []}
        offset={4}
      />
    );
  }
  function renderGridPreview(dashboard) {
    return <GridPreview widgetPreview={dashboard.widgetPreview} />;
  }

  // TODO(__SENTRY_USING_REACT_ROUTER_SIX): We can remove this later, react
  // router 6 handles empty query objects without appending a trailing ?
  const queryLocation = {
    ...(location.query && Object.keys(location.query).length > 0
      ? {query: location.query}
      : {}),
  };

  function renderMiniDashboards() {
    return dashboards?.map((dashboard, index) => {
      return (
        <DashboardCard
          key={`${index}-${dashboard.id}`}
          title={dashboard.title}
          to={{
            pathname: `/organizations/${organization.slug}/dashboard/${dashboard.id}/`,
            ...queryLocation,
          }}
          detail={tn('%s widget', '%s widgets', dashboard.widgetPreview.length)}
          dateStatus={
            dashboard.dateCreated ? <TimeSince date={dashboard.dateCreated} /> : undefined
          }
          createdBy={dashboard.createdBy}
          renderWidgets={() => renderGridPreview(dashboard)}
          renderContextMenu={() => renderDropdownMenu(dashboard)}
        />
      );
    });
  }

  function renderDashboardGrid() {
    if (!dashboards?.length) {
      return (
        <EmptyStateWarning>
          <p>{t('Sorry, no Dashboards match your filters.')}</p>
        </EmptyStateWarning>
      );
    }
    return <DashboardGrid>{renderMiniDashboards()}</DashboardGrid>;
  }

  return (
    <Fragment>
      {renderDashboardGrid()}
      <PaginationRow
        pageLinks={pageLinks}
        onCursor={(cursor, path, query, direction) => {
          const offset = Number(cursor?.split?.(':')?.[1] ?? 0);

          const newQuery: Query & {cursor?: string} = {...query, cursor};
          const isPrevious = direction === -1;

          if (offset <= 0 && isPrevious) {
            delete newQuery.cursor;
          }
          trackAnalytics('dashboards_manage.paginate', {organization});

          navigate({
            pathname: path,
            query: newQuery,
          });
        }}
      />
    </Fragment>
  );
}

const DashboardGrid = styled('div')`
  display: grid;
  grid-template-columns: minmax(100px, 1fr);
  grid-template-rows: repeat(3, max-content);
  gap: ${space(2)};

  @media (min-width: ${p => p.theme.breakpoints.small}) {
    grid-template-columns: repeat(2, minmax(100px, 1fr));
  }

  @media (min-width: ${p => p.theme.breakpoints.large}) {
    grid-template-columns: repeat(3, minmax(100px, 1fr));
  }
`;

const PaginationRow = styled(Pagination)`
  margin-bottom: ${space(3)};
`;

const DropdownTrigger = styled(Button)`
  transform: translateX(${space(1)});
`;

export default withApi(DashboardList);
